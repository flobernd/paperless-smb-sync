<!-- markdownlint-disable-file MD033 MD041 -->
<div align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/logo-dark.png">
    <img src="assets/logo-light.png" alt="paperless-smb-sync" width="360">
  </picture>
</div>

`paperless-smb-sync` is a single container that serves per-user SMB inbox
shares for a [multi-user paperless-ngx setup](https://blog.flobernd.de/2026/02/paperless-ngx-document-management/)
and keeps those shares in sync with paperless group membership. Add a user to
a paperless group and the container creates their Samba account, their inbox
folder, and a consumption workflow that files scans under their name; remove
them from the group and it tears the same things back down.

## How it works

### The reconcile loop

The container runs `smbd` in the foreground and a Python sync daemon in the
background. Every `POLL_INTERVAL` seconds the daemon fetches the desired
state from the paperless API (who is a member of `SYNC_GROUP` and whether
each member is active) and the actual state on disk (Samba accounts, inbox
folders, managed workflows), then applies whatever actions close the gap.
Reconciliation is level-based: every action is idempotent, so a crashed or
partially applied cycle is simply completed by the next one, and a `smbd`
crash never stops the sync daemon or vice versa.

### Opt-in by group membership

Only paperless users who belong to `SYNC_GROUP` (default `smb`) are
provisioned. Everyone else, including admin and service accounts, is left
alone. Membership is edited entirely in the paperless UI; there is no
separate allowlist to maintain.

### Managed workflows

Each provisioned user gets a consumption workflow named `[smb-sync]
<username>` that assigns ownership and view/edit permissions on documents
consumed from that user's inbox folder. The sync daemon only ever touches
workflows carrying that prefix, so hand-created workflows are never
modified or deleted.

### Passwords are generated and shown once

The daemon generates a random 20-character password when it creates a Samba
account and prints it to the container log exactly once, in a block with the
server, share path, username, and password ready to copy into a scanner
profile. Nothing else keeps a copy: only Samba's own `passdb.tdb` stores the
(hashed) credential, and it is not retrievable in plaintext afterward. If a
password is lost, generate a new one with `smb-sync regen <username>` (see
Admin commands below).

### Credentials can be emailed to the user

Set `EMAIL_CREDENTIALS=true` and the daemon also sends the block to the user's
paperless email address, both when the account is provisioned and when
`smb-sync regen` sets a new password. Onboarding then does not depend on an
administrator reading the log and relaying a password by hand.

Delivery is best effort and never blocks provisioning: the block is always
printed as well, and a mail server that rejects or drops the message is logged
as an error rather than retried forever. Two situations are worth watching for
in the log. Enabling the setting without `PAPERLESS_EMAIL_HOST` warns once at
startup and nothing is ever sent, and a user with no email address in
paperless warns every time credentials are generated for them.

### The only state is passdb.tdb

The `/var/lib/samba` volume holds `passdb.tdb`, Samba's own credential store,
and must persist across restarts; without it every user gets a new password
after a container recreation. The unix accounts backing those logins live in
the container's ephemeral filesystem, and the daemon rebuilds each missing one
from its surviving `passdb.tdb` entry on the next sync cycle.

### Deactivation suspends, it does not delete

Setting a paperless user's account to inactive (without removing them from
the group) disables their Samba login but leaves their inbox folder and
workflow in place; reactivating the user re-enables the account with the
same data intact. This protects against a temporary deactivation being
mistaken for an offboarding.

### Removing a user deletes their inbox folder and its contents

Leaving `SYNC_GROUP` or being deleted in paperless is treated as
offboarding: the daemon deletes the user's Samba account, their `[smb-sync]`
workflow, and their inbox folder, **including any files still sitting in
it**. Make sure documents are consumed (or moved elsewhere) before removing
someone from the group. To lose only the login and keep the data, deactivate
the paperless user instead of removing them from the group.

### Username requirements

A paperless username is only provisioned if it is also a valid unix account
name: lowercase letters, digits, underscores, and hyphens, starting with a
letter or underscore, at most 32 characters (`^[a-z_][a-z0-9_-]{0,31}$`).
Usernames that fail this check are skipped with a logged warning and are
never partially provisioned; renaming the user in paperless to a valid name
resolves it on the next cycle.

### Renames

Because the Samba UID is derived from the paperless user id, a username
change is recognized as a rename rather than a delete followed by a create.
The unix account, Samba account, inbox folder, and workflow are all renamed
in place; the password and existing documents are untouched. The daemon logs
a reminder that the scanner profile's folder path has to be updated by hand
after a rename.

## Configuration

All configuration is via environment variables, read in
`src/smb_sync/config.py`.

| Variable | Default | Meaning |
| --- | --- | --- |
| `PAPERLESS_URL` | required | Base URL of the paperless instance, e.g. `http://webserver:8000`. |
| `PAPERLESS_API_TOKEN` / `PAPERLESS_API_TOKEN_FILE` | required (one of the two) | Admin API token, given directly or as a path to a file containing it. Setting both is a startup error. |
| `SYNC_GROUP` | `smb` | Paperless group whose members are provisioned. |
| `POLL_INTERVAL` | `30` | Seconds between reconcile cycles. |
| `UID_BASE` | `20000` | Base for the unix UID of managed accounts; a user's UID is `UID_BASE` plus their paperless user id. |
| `PAPERLESS_GID` | `10000` | GID of the shared `paperless` group that owns every inbox folder. |
| `SHARE_NAME` | `inbox` | Name of the exported SMB share. |
| `SMB_HOST` | empty | Hostname or IP shown in the printed credentials block (provisioning log and `smb-sync regen` output); display only, it does not affect how the share is served. |
| `EMAIL_CREDENTIALS` | `false` | Email each credentials block to the user in addition to printing it. Needs the SMTP settings below. |
| `INBOX_PATH` | `/shares/inbox` | Path inside the container where the sync daemon creates and manages per-user inbox folders. The share definition and the container's `mkdir` at startup both use the fixed path `/shares/inbox`, so this normally only needs to match the volume mount target and does not need to be changed. |

The admin API token needs permission to read users, groups, and workflows,
and to create, update, and delete workflows; an administrator account's
token is the simplest choice.

### SMTP settings

`EMAIL_CREDENTIALS` reads its mail server from the same variables paperless-ngx
itself uses, so a deployment that already sends mail can hand this container
the same values, or the same `env_file`, instead of configuring a second time:
`PAPERLESS_EMAIL_HOST`, `PAPERLESS_EMAIL_PORT` (default `25`),
`PAPERLESS_EMAIL_HOST_USER`, `PAPERLESS_EMAIL_HOST_PASSWORD`,
`PAPERLESS_EMAIL_USE_TLS`, `PAPERLESS_EMAIL_USE_SSL`, and
`PAPERLESS_EMAIL_FROM` (defaults to `PAPERLESS_EMAIL_HOST_USER`).

Only `PAPERLESS_EMAIL_HOST` is required; it has no default here, and leaving it
unset is what disables delivery.

## Custom Samba configuration

The container generates `/etc/samba/smb.conf` on every start, so edits to it
do not survive a restart. To add your own Samba settings, mount one or more
`*.conf` files into `/etc/samba/conf.d/`:

```yaml
    volumes:
      - ./smb-custom.conf:/etc/samba/conf.d/custom.conf:ro
```

Drop-ins are appended to the generated file in filename order, after the
managed share. A `[global]` header in a drop-in therefore re-opens the global
section and its settings override the defaults, and any other section header
defines an additional share. The container runs `testparm` over the merged
file at startup and refuses to start if the result contains an unknown
parameter, so a misspelled setting shows up immediately in the log instead of
being ignored for the life of the container.

A drop-in that repeats the managed share's section name (`inbox`, or whatever
`SHARE_NAME` is set to) merges into it rather than replacing it, so that is
the supported way to add settings to the share itself. Do not override its
`path` or `valid users` there: those are what route each user into their own
inbox folder, and changing them breaks provisioning for every user.

### Making the inbox write-once

Anyone holding a user's SMB credentials can overwrite or delete that user's
scans while they wait to be consumed. Samba's `worm` module closes that off by
turning a file read-only once it is older than a grace period:

```yaml
[inbox]
    vfs objects = worm
    worm:grace_period = 300
```

New files can still be created, so scanning keeps working, and paperless
reaches the folder through the filesystem rather than over SMB, so consumption
and cleanup are unaffected. Only access arriving over SMB is restricted.

Keep the grace period long enough for a scan job to finish. Scanners that
write to a temporary name and rename it, or that reopen a file to append
pages, fail against a file that has already frozen; five minutes is a safe
starting point, `0` is not.

## Setup

1. In paperless, create the `smb` group (or whatever name you plan to use
   for `SYNC_GROUP`).
2. Create an admin API token in paperless (user profile settings) for the
   container to use as `PAPERLESS_API_TOKEN`.
3. Copy `compose.example.yaml` to `compose.yaml`, set `PAPERLESS_URL` and
   `PAPERLESS_API_TOKEN` (or mount a secrets file and use
   `PAPERLESS_API_TOKEN_FILE` instead), and point the `/shares/inbox` volume
   at the same host directory the paperless consumer watches.
4. Start it: `docker compose up -d --build`.
5. Add users to the `smb` group in paperless.
6. Read each user's one-time credentials block with `docker logs
   <container>` and enter the printed server, share path, username, and
   password into the scanner's SMB profile.

Each user is provisioned within one `POLL_INTERVAL` of joining the group;
the credentials block appears in the container log at that moment and is
not printed again.

## Admin commands

Run these against the running container with `docker exec <container>
smb-sync <command>`:

- `status` - lists every managed user with their UID and the state of their
  Samba account, inbox folder, and workflow.
- `sync` - runs one reconcile cycle immediately, instead of waiting for the
  next `POLL_INTERVAL`.
- `regen <username>` - sets a new random password for a managed account and
  prints the one-time credentials block again. Fails if the account is not
  one the daemon manages.

## License

Released under the MIT license. See [`LICENSE`](LICENSE) for the full text.
