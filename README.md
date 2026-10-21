# Event-Driven IAM Security Automation on AWS

**IAM user deletion detection, automated recovery, quarantine, and preventive controls using AWS services.**

## Overview

This project monitors IAM user deletion activity and automatically responds when a matching event occurs. CloudTrail records the `DeleteUser` API call, EventBridge invokes a Lambda function, and Lambda uses an IAM user snapshot stored in DynamoDB to attempt restoration. The function also applies a quarantine policy to the user responsible and sends an alert through Amazon SNS.

An additional IAM deny policy helps prevent members of a protected IAM group from deleting other IAM users in the first place.

## Architecture

![AWS IAM security architecture](architecture/architecture.png)

The architecture has two EventBridge triggers: one for IAM `DeleteUser` events and another that refreshes DynamoDB snapshots every hour. A separate IAM policy adds prevention.

[View architecture and event flow](docs/architecture.md)

## Key Features

- **IAM deletion detection:** CloudTrail and EventBridge identify matching `DeleteUser` activity.
- **Hourly snapshots:** Lambda saves IAM user configuration to DynamoDB.
- **Automated response:** Lambda attempts to recreate the deleted user from a saved snapshot.
- **Quarantine:** The actor receives the `quarantine-no-user-deletion` policy.
- **Notifications:** SNS sends incident alerts to a confirmed email subscription.
- **Prevention:** A group-attached IAM deny policy blocks selected deletion and access-removal operations.

## AWS Services

| Service | Purpose |
| --- | --- |
| IAM | User management, Lambda execution role, quarantine and preventive policies |
| CloudTrail | Records IAM API activity |
| EventBridge | Triggers deletion response and hourly snapshots |
| Lambda | Runs the snapshot and response code |
| DynamoDB | Stores IAM user snapshots |
| SNS | Sends email alerts |
| CloudWatch Logs | Shows Lambda execution logs |
| S3 | Stores CloudTrail logs |

## Project Configuration

| Item | Value |
| --- | --- |
| AWS Region | `us-east-1` |
| Lambda | `protect-iam-user-deletion` |
| Runtime / handler | Python 3.12 / `lambda_function.handler` |
| DynamoDB table | `iam-user-snapshots` |
| SNS topic | `iam-user-deletion-alerts` |
| EventBridge deletion rule | `protect-iam-user-deletion-rule` |
| Snapshot schedule | `rate(1 hour)` |
| Preventive policy | `deny-deleting-other-iam-users` |

## Testing Results

Testing used `lab-admin` and `test-victim` IAM users. DynamoDB snapshots captured seven user configurations. A simulated `DeleteUser` event returned a `restored` status, and the live deletion test restored `test-victim` and applied the quarantine policy to `lab-admin`. A later deletion attempt by `lab-admin` was denied.

During testing, CloudTrail Event history showed the deletion but Lambda did not receive the event. The missing CloudTrail trail was identified and configured to log management events, after which the event-driven response worked.

[View testing and screenshots](docs/testing.md)

## Repository Structure

```text
aws-event-driven-iam-security/
├── README.md
├── .gitignore
├── lambda/
│   └── lambda_function.py
├── architecture/
│   ├── architecture.drawio
│   ├── architecture.png
│   └── policies/
│       ├── lambda-execution-policy.json
│       └── deny-deleting-other-iam-users.json
├── docs/
│   ├── architecture.md
│   ├── implementation.md
│   └── testing.md
└── screenshots/
    └── [screenshots]
```

## Documentation

- [Architecture](docs/architecture.md)
- [Implementation](docs/implementation.md)
- [Testing and screenshots](docs/testing.md)
- [Lambda code](lambda/lambda_function.py)
- [Lambda execution policy](architecture/policies/lambda-execution-policy.json)
- [Preventive IAM policy](architecture/policies/deny-deleting-other-iam-users.json)
