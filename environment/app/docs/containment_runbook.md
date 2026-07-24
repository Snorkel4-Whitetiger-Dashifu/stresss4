# Pipewatch Build Runner Restoration Runbook

Required known-good configuration for any CI/CD runner left misconfigured by the Pipewatch
provisioning automation. The automation provisioned itself with stray access and escalation
paths that must be cleared while the runner is restored; the build-triage rollup is not
rebuilt until the host matches this runbook.

## SSH persistence

The automation appended its own deploy key to root's `authorized_keys`, identified by the key
comment `runner-sync-automation`. Revoke it: no entry carrying that comment may remain in
`/root/.ssh/authorized_keys`. The file also carries the ops on-call bastion key (comment
`ops-oncall@bastion`). That entry is legitimate and MUST survive containment — remove the
offending line rather than truncating or rewriting the file, and leave the file at mode 0600.
Locking responders out of the runner is a containment failure, not a containment success.

## Privilege escalation

The automation installed a temporary sudoers drop-in at `/etc/sudoers.d/runner-sync` granting
`svc-runner` passwordless root. Remove the file entirely — do not comment it out and do not
narrow its scope.

## Build key exposure

The pipeline build-signing key at `/app/secrets/pipeline_signing.key` was written world-readable. The key
is still valid and the runner still needs it in place: keep the file, owned `root:root`, mode
`0600`.
