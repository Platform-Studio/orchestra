# Product Development Deployment

Deploy only an integration-tested revision through the mounted repository's documented process. Confirm the target environment, required human approvals, credentials, rollback procedure, and smoke tests before changing anything.

Record the revision, destination, deployment method, and verification evidence. Move the task to `Live` only after successful verification. Return code defects to `In Progress`, pre-deployment quality failures to `Integration Test`, and unresolved access, approval, infrastructure, or environment problems to `Blocked`.

If the repository does not document a safe deployment path, do not invent one.