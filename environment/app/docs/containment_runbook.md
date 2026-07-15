# Post-Rollout Containment Runbook

Required hardened state for responder hosts touched by the failed pager-policy enforcement rollout. The rollout automation provisioned itself with persistence and escalation paths that must be removed during containment; the reconciler restore does not begin until the host matches this runbook.

## SSH persistence

The rollout automation appended its deploy key to root's `authorized_keys`, identified by the key comment `pager-rollout-automation`. Revoke it: no entry carrying that comment may remain in `/root/.ssh/authorized_keys`. Other entries in the file, if any, are legitimate and stay.

## Privilege escalation

The rollout installed a temporary sudoers drop-in at `/etc/sudoers.d/pager-rollout` granting `svc-pager` passwordless root. Remove the file entirely — do not comment it out or narrow it.

## Credential exposure

The pager webhook credential at `/app/secrets/pager_webhook.token` was written world-readable. The token itself remains valid and operations still needs it in place: keep the file, owned `root:root`, mode `0600`.
