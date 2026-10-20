import json
import logging
import os
from datetime import datetime, timezone

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_cfg = Config(retries={"max_attempts": 5, "mode": "standard"})
iam = boto3.client("iam", config=_cfg)
sns = boto3.client("sns", config=_cfg)
table = boto3.resource("dynamodb", config=_cfg).Table(os.environ["TABLE_NAME"])

SNS_TOPIC_ARN = os.environ["SNS_TOPIC_ARN"]
# Comma-separated IAM user names that ARE allowed to delete users (optional)
ALLOWED_DELETERS = {
    n.strip() for n in os.environ.get("ALLOWED_DELETERS", "").split(",") if n.strip()
}
# "true" = only report, never restore or quarantine (use while testing)
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

QUARANTINE_POLICY = "quarantine-no-user-deletion"
QUARANTINE_DOC = {
    "Version": "2012-10-17",
    "Statement": [{
        "Sid": "BlockedAfterDeletingAnIAMUser",
        "Effect": "Deny",
        "Action": [
            "iam:DeleteUser", "iam:DeleteLoginProfile", "iam:DeleteAccessKey",
            "iam:UpdateAccessKey", "iam:DetachUserPolicy", "iam:DeleteUserPolicy",
            "iam:PutUserPolicy", "iam:RemoveUserFromGroup",
            "iam:DeactivateMFADevice", "iam:DeleteVirtualMFADevice",
        ],
        "Resource": "*",
    }],
}


def _err(e):
    return e.response.get("Error", {}).get("Code", "")


def _all(operation, key, **kwargs):
    items = []
    for page in iam.get_paginator(operation).paginate(**kwargs):
        items.extend(page[key])
    return items


# ---------- Snapshot: remember every user's configuration ----------

def snapshot_user(name):
    user = iam.get_user(UserName=name)["User"]
    try:
        iam.get_login_profile(UserName=name)
        console = True
    except ClientError as e:
        if _err(e) != "NoSuchEntity":
            raise
        console = False
    inline = {}
    for policy in _all("list_user_policies", "PolicyNames", UserName=name):
        doc = iam.get_user_policy(UserName=name, PolicyName=policy)["PolicyDocument"]
        inline[policy] = doc if isinstance(doc, str) else json.dumps(doc)
    return {
        "user_name": name,
        "path": user.get("Path", "/"),
        "permissions_boundary": (user.get("PermissionsBoundary") or {}).get("PermissionsBoundaryArn"),
        "tags": _all("list_user_tags", "Tags", UserName=name),
        "groups": [g["GroupName"] for g in _all("list_groups_for_user", "Groups", UserName=name)],
        "managed_policies": [p["PolicyArn"] for p in
                             _all("list_attached_user_policies", "AttachedPolicies", UserName=name)],
        "inline_policies": json.dumps(inline),
        "had_console_password": console,
        "access_key_count": len(iam.list_access_keys(UserName=name)["AccessKeyMetadata"]),
        "mfa_device_count": len(iam.list_mfa_devices(UserName=name)["MFADevices"]),
        "snapshot_at": datetime.now(timezone.utc).isoformat(),
    }


def snapshot_all():
    names = [u["UserName"] for u in _all("list_users", "Users")]
    saved = 0
    with table.batch_writer() as batch:
        for name in names:
            try:
                batch.put_item(Item=snapshot_user(name))
                saved += 1
            except ClientError as e:
                if _err(e) != "NoSuchEntity":  # user deleted mid-snapshot
                    raise
    logger.info("Snapshot saved for %d users", saved)
    return {"status": "snapshot", "users": saved}


# ---------- Response: contain the actor, restore the user ----------

def quarantine(actor):
    iam.put_user_policy(UserName=actor, PolicyName=QUARANTINE_POLICY,
                        PolicyDocument=json.dumps(QUARANTINE_DOC))


def restore_user(snap):
    name = snap["user_name"]
    kwargs = {"UserName": name, "Path": snap.get("path") or "/"}
    if snap.get("tags"):
        kwargs["Tags"] = snap["tags"]
    if snap.get("permissions_boundary"):
        kwargs["PermissionsBoundary"] = snap["permissions_boundary"]
    iam.create_user(**kwargs)

    problems = []
    for group in snap.get("groups") or []:
        try:
            iam.add_user_to_group(GroupName=group, UserName=name)
        except ClientError as e:
            problems.append(f"group {group}: {_err(e)}")
    for arn in snap.get("managed_policies") or []:
        try:
            iam.attach_user_policy(UserName=name, PolicyArn=arn)
        except ClientError as e:
            problems.append(f"managed policy {arn}: {_err(e)}")
    for policy, doc in json.loads(snap.get("inline_policies") or "{}").items():
        try:
            iam.put_user_policy(UserName=name, PolicyName=policy, PolicyDocument=doc)
        except ClientError as e:
            problems.append(f"inline policy {policy}: {_err(e)}")
    return problems


def notify(subject, message):
    # SNS Subject must be ASCII, no line breaks, under 100 chars
    sns.publish(TopicArn=SNS_TOPIC_ARN, Subject=subject[:99], Message=message)


def handler(event, context):
    # New-style EventBridge subscribers send a list of events; rules send one
    if isinstance(event, list):
        return [handler(e, context) for e in event]
    logger.info("Event received: %s", json.dumps(event))

    # Hourly schedule (or a manual test event) refreshes the snapshot
    if event.get("action") == "snapshot" or event.get("detail-type") == "Scheduled Event":
        return snapshot_all()

    detail = event.get("detail") or {}
    identity = detail.get("userIdentity") or {}
    deleted = (detail.get("requestParameters") or {}).get("userName")

    if detail.get("eventName") != "DeleteUser" or not deleted:
        return {"status": "skipped", "reason": "not a DeleteUser event"}
    if detail.get("errorCode"):
        return {"status": "skipped", "reason": f"DeleteUser failed: {detail['errorCode']}"}
    # Only IAM users are restricted; roles, root and SSO sessions are allowed
    if identity.get("type") != "IAMUser":
        return {"status": "allowed", "reason": f"deleter type {identity.get('type')}"}

    actor = identity.get("userName", "unknown")
    if actor in ALLOWED_DELETERS:
        return {"status": "allowed", "reason": f"{actor} is an allowed deleter"}

    summary = (
        f"IAM user '{actor}' deleted the IAM user '{deleted}'.\n"
        f"Deleting IAM users is against organization policy. "
        f"Only approved administrator roles may remove IAM identities.\n\n"
        f"Account: {detail.get('recipientAccountId')}\n"
        f"Time: {detail.get('eventTime')}\n"
        f"Source IP: {detail.get('sourceIPAddress')}\n"
        f"Deleter ARN: {identity.get('arn')}\n"
    )
    snap = table.get_item(Key={"user_name": deleted}).get("Item")

    if DRY_RUN:
        notify("POLICY VIOLATION: IAM user deleted (dry run)",
               summary + "\nAction: none (DRY_RUN=true). The user was not restored.")
        return {"status": "dry-run", "user": deleted, "actor": actor}

    actions, problems = [], []

    def contain():
        try:
            quarantine(actor)
            actions.append(f"Containment: deny policy '{QUARANTINE_POLICY}' added to '{actor}'. "
                           f"They can no longer delete or strip IAM users.")
        except ClientError as e:
            problems.append(f"quarantine of {actor} failed: {_err(e)}")

    if actor != deleted:
        contain()  # stop further deletions first

    if not snap:
        problems.append(f"No snapshot exists for '{deleted}', so it could not be restored. "
                        f"Recreate it manually.")
    else:
        try:
            for p in restore_user(snap):
                problems.append(f"restore: {p}")
            actions.append(f"Restore: '{deleted}' recreated with its groups, policies and tags "
                           f"from the snapshot taken {snap.get('snapshot_at')}.")
            actions.append(
                f"NOT restorable: console password ({'yes' if snap.get('had_console_password') else 'none'}), "
                f"access keys ({snap.get('access_key_count', 0)}), MFA devices ({snap.get('mfa_device_count', 0)}). "
                f"An administrator must reset the password and issue new credentials.")
        except ClientError as e:
            if _err(e) == "EntityAlreadyExists":
                actions.append(f"Restore skipped: a user named '{deleted}' already exists again.")
            else:
                problems.append(f"restore of {deleted} failed: {_err(e)}")

    if actor == deleted:
        contain()  # the deleter removed themselves; lock the restored user

    body = summary + "\n" + "\n".join(f"- {a}" for a in actions)
    if problems:
        body += "\n\nPROBLEMS:\n" + "\n".join(f"- {p}" for p in problems)
        logger.error("Problems: %s", problems)
        notify("ACTION NEEDED: IAM user deletion not fully reverted", body)
        return {"status": "partial", "user": deleted, "actor": actor, "problems": problems}

    notify("POLICY VIOLATION: IAM user deleted and restored", body)
    return {"status": "restored", "user": deleted, "actor": actor}
