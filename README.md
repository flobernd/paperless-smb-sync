# paperless-smb-sync

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

Paperless passwords cannot be reused for Samba: paperless stores a Django
PBKDF2 hash and Samba needs an NT hash, and one cannot be derived from the
other. Instead, the daemon generates a random 20-character password when it
creates a Samba account and prints it to the container log exactly once, in
a block with the server, share path, username, and password ready to copy
into a scanner profile. Nothing else keeps a copy: only Samba's own
`passdb.tdb` stores the (hashed) credential, and it is not retrievable in
plaintext afterward. If a password is lost, generate a new one with `smb-sync
regen <username>` (see Admin commands below).

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
| `SMB_HOST` | empty | Hostname or IP shown in the printed credentials block and in `smb-sync status`; display only, it does not affect how the share is served. |
| `INBOX_PATH` | `/shares/inbox` | Path inside the container where the sync daemon creates and manages per-user inbox folders. The share definition and the container's `mkdir` at startup both use the fixed path `/shares/inbox`, so this normally only needs to match the volume mount target and does not need to be changed. |

The admin API token needs permission to read users, groups, and workflows,
and to create and delete workflows; an administrator account's token is the
simplest choice.

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
5. Add users to the `smb` group in paperless. Within one `POLL_INTERVAL`,
   each newly added user is provisioned and their one-time credentials block
   is written to the container log; read it with `docker logs <container>`.
6. Enter the printed server, share path, username, and password into the
   scanner's SMB profile for that user.

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

## Migrating from the servercontainers/samba setup

The [referenced blog post](https://blog.flobernd.de/2026/02/paperless-ngx-document-management/)
describes a setup using the `servercontainers/samba` image with manually
maintained accounts. Moving to this container is a one-time procedure:

1. Create the `smb` group in paperless and add the existing scanner users to
   it.
2. Create an admin API token as described in Setup above.
3. In your compose file, replace the `servercontainers/samba` service with
   this one, keep the existing bind mount to the shared inbox directory, and
   add the named volume for `/var/lib/samba` shown in
   `compose.example.yaml`. This volume holds `passdb.tdb`, Samba's own
   credential store, and must persist across restarts.
4. Start the container. Because existing paperless passwords cannot be
   reused for Samba, the first reconcile cycle generates a new password for
   every existing user and prints their credentials blocks to the log as if
   they were newly provisioned. Update each scanner profile once with the
   new password.

Existing documents and inbox folders are not touched by the migration; only
the account and password layer changes.
