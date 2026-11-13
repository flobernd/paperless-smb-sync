<!-- markdownlint-disable-file MD033 MD041 -->
<div align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/logo-dark.png">
    <img src="assets/logo-light.png" alt="paperless-smb-sync" width="360">
  </picture>
</div>

`paperless-smb-sync` is a single container that serves per-user SMB inbox shares for a [multi-user paperless-ngx
setup](https://blog.flobernd.de/2026/02/paperless-ngx-document-management/) and keeps those shares in sync with
paperless group membership. Add a user to a paperless group and the container creates their Samba account, their inbox
folder, and a consumption workflow that files scans under their name. Remove them from the group and it tears the same
things back down.

The steps to run it are under [Setup](#setup). When you upgrade from 0.1.0, read [Upgrading from
0.1.0](#upgrading-from-010) first, since this version refuses to start on the Samba volume of 0.1.0.

## How it works

### The reconcile loop

The container runs `smbd` in the foreground and a Python sync daemon in the background. The daemon runs a cycle, waits
`POLL_INTERVAL` seconds and then runs the next one. A cycle fetches the desired state from the paperless API (who is a
member of `SYNC_GROUP` and whether each member is active) and the actual state (Samba accounts, inbox folders, managed
workflows), then applies whatever actions close the gap. Reconciliation is level-based: each cycle plans from the state
it reads, so the next cycle completes one that crashed or stopped half way.

`smbd` is the main process of the container, so its exit stops the container. When the daemon exits, the entrypoint
starts it again after five seconds.

The daemon logs each warning when its cause first appears, and again once per hour while the cause lasts. A manual
`smb-sync sync` logs each warning of its cycle.

### Start and health

The container exits at start when its configuration is not valid: `PAPERLESS_URL` is missing, the token is missing or
empty or cannot be read, both token variables are set, a numeric setting is not a number, `PAPERLESS_GID` or
`POLL_INTERVAL` is below 1, `UID_BASE` is below 1000, `CREDENTIALS_DOCUMENT` has an unknown value, or
`PAPERLESS_CONSUMPTION_DIR` is not valid. It also exits when Samba rejects the merged `smb.conf`, as described under
Custom Samba configuration below, and when the Samba volume was made with another `UID_BASE` or holds accounts of an
earlier version, as described under The Samba volume holds the state below. `PAPERLESS_GID` can also be the id of a
group that the image already has, such as 100.

`docker ps` shows a health state. The container turns unhealthy when the daemon has completed no cycle for three poll
intervals, or for three minutes when that is longer. Docker shows that state after three failed checks, which run 30
seconds apart. A cycle that fails counts as completed, so an unreachable paperless does not make the container
unhealthy. A cycle that runs longer than this threshold reads unhealthy until it ends. Docker does not restart an
unhealthy container on its own.

### Opt-in by group membership

Only paperless users who belong to `SYNC_GROUP` (default `smb`) are provisioned. Other users, including admin and
service accounts, are left alone. Membership is edited in the paperless UI, and there is no separate allowlist to
maintain.

### Accounts and inbox folders

The unix uid of a user is `UID_BASE` plus the paperless user id. The unix account, the Samba entry, and the inbox folder
of the user have the account name, which is `u` followed by the uid, such as `u20005` for the user with the id 5 under
the default `UID_BASE`. The unix accounts have the group `smb-sync` and are not members of the group of paperless. The
share gives each file the group of paperless.

An inbox folder has the uid as its owner, the group id `PAPERLESS_GID`, and the mode 2770. The daemon repairs a folder
whose owner or group differs, and it also sets the group of each file below the folder. So after a change of
`PAPERLESS_GID`, the next cycle gives the folders and their files the new group.

### Login names

A person logs in with the paperless username. The file `/var/lib/samba/smb-sync-usermap` on the Samba volume maps each
login name to its account, and Samba refuses a login under an account name. A username gets no login name when it starts
with `@`, `+`, or `&`, which Samba reads in the map as a group or a netgroup, or when it holds a character other than
letters, digits, `_`, `.`, `@`, `+`, and `-`.

Samba compares login names without case. Of several members whose usernames are equal without case, one keeps the name:
the member who has it now, and otherwise the one with the lowest user id. A member without a login name gets a warning
and no account. A member who already has an account keeps it, together with the folder, and has no login until its name
changes, or until the member who keeps the name renames or leaves the group.

### Renames

The daemon handles a rename in paperless with one write of the map in the next cycle. The password, the folder, and the
workflow stay, and the user types the new name at the login. A swap of two names also takes one write of the map. A
member who takes over the name of a former member logs in with it to an account of their own, with its own password.

### Managed workflows

Each provisioned user gets a consumption workflow named `[smb-sync] <account name>` that assigns ownership and view/edit
permissions on documents consumed from that user's inbox folder. The sync daemon only touches workflows carrying that
prefix, so it does not modify or delete a hand-created workflow without it.

The filter of a managed workflow is `<PAPERLESS_CONSUMPTION_DIR>/<account name>/*`, the user's inbox folder as the
paperless container sees it. Paperless compares the filter with the resolved path of a scan, so
`PAPERLESS_CONSUMPTION_DIR` has to be the resolved path as well, with no symbolic link in it. The smb container cannot
follow a link of the paperless container, so it cannot resolve the path itself.

The inbox folders belong to the group with the id `PAPERLESS_GID`, so paperless needs that group id to read them. In the
official paperless-ngx image, `USERMAP_GID` sets it.

In each cycle the daemon compares a managed workflow with its definition: the enabled flag, the single trigger (its
type, source, and path filter, and its file name filter and mail rule, which must be empty), the single action (its type
and owner), the view and the change users, and the view and the change groups, which are empty. When one of them
differs, the daemon writes the full definition back, and a field that it does not set and does not compare, such as a
tag of the action, is lost then.

The daemon opens the login of an account only while the member is active, the workflow exists and matches that
definition, and the inbox folder of the account is a directory with the uid as its owner. The workflow counts since
paperless imports a scan without it too, and only the workflow gives the scan its owner. The daemon checks this once in
each cycle, so after a deactivation or the deletion of the workflow the login stays open until the next cycle. A new
account opens in the cycle after its creation, and a workflow that a person deleted, disabled, or changed in another of
these fields closes the login for one cycle. An inbox folder that is gone, that is a link, or that has another owner
closes the login until a cycle finds the folder again, and the daemon makes or repairs the folder where it can.

To give other users or groups a right on each scan of a user, make a second workflow without the prefix, with a
consumption trigger, the path filter of the inbox folder, and an action that gives the right and sets no owner.
Paperless adds up the view and the change users and groups of each workflow that matches. `smb-sync status` shows the
account name for the filter.

### Closed logins

The daemon closes a login for an entry of another paperless user, for an inactive member, at the start of a removal,
instead of a removal during a hold (see Entries of another paperless database below), and for a workflow that is gone or
changed. Samba keeps a session that started before, so the daemon also ends the open SMB sessions of the account when it
closes its login. A session that is left on a closed login, for example after a stop that failed, ends in the next
cycle. The stop ends the `smbd` process of the client connection, so a session of another account on the same connection
ends with it, and that client connects again.

### Passwords are generated and never logged

The daemon generates a random 20-character password of letters and digits when it creates a Samba account, and no log
holds a password. Samba's own `passdb.tdb` stores the hashed credential, and the password cannot be retrieved from it in
plaintext afterward. A user gets the first password through the credentials document below, or the administrator runs
`docker exec <container> smb-sync regen <login name>`, which sets a new password and prints the block with the server,
share path, username, and password to the terminal, ready to copy into a scanner profile. When a password is lost, the
same command sets a new one (see Admin commands below).

### Credentials can go to the user as a document

Set `CREDENTIALS_DOCUMENT=true` and the daemon uploads the block of a new account to paperless as a text document. Once
paperless has imported it, the next cycle hands the document to the user under the title
`[smb-sync] credentials <login name>`, which leaves only that user and a superuser able to read it. Until the hand-over
the title also carries the id of the user. Onboarding then does not depend on an administrator running `smb-sync regen`
and relaying a password by hand.

The option needs the token of a superuser. With another token the daemon logs a warning and makes no document. Set
`SMB_HOST` as well when the option is on. Without it the block shows a literal `<server>` placeholder, which an
administrator at the terminal can resolve but the user reading the document cannot.

`smb-sync regen` first sets the rights of the document back to the user alone, which ends a share that the user set on
it. It then puts the new block on the same document as a new version. Earlier versions stay in the history of the
document, with passwords that Samba rejects. When a member who has a login name is renamed, the daemon changes the title
of the document and adds a note. While the option is on and the token belongs to a superuser, the daemon deletes the
document when the account goes and empties it from the trash. Under the same condition it also deletes a credentials
document that has no owner, since each paperless user can read such a document. The daemon counts a document as a
credentials document only when it has such a title and the file name `smb-credentials.txt` of its upload, which the API
cannot change, so a document that a user retitled stays.

A paperless workflow that assigns an owner or rights to documents uploaded through the API, or to documents that are
updated, can give the password to another user. Keep the credentials documents out of such a workflow. An upload that
waits for its user but that the user of the token does not own stays untouched, with a warning.

A failed upload is logged and not tried again. An upload that paperless accepts and then fails to import shows in the
tasks of paperless and not in the log of the daemon. In both cases `smb-sync regen` makes a new document. When paperless
does not answer the requests for the documents, the cycle still creates and removes accounts, but it makes no document.
The document of an account that such a cycle removes stays in paperless, with the password of an account that is gone.
The document holds the password in plaintext, and the full text search of that user finds it. With the AI features of
paperless on, paperless can also put the text of a credentials document into its AI index.

### The Samba volume holds the state

The `/var/lib/samba` volume holds Samba's own credential store `passdb.tdb`, the file `smb-sync-uid-base`, and the
username map `smb-sync-usermap`, and must persist across restarts. Without it a recreation of the container loses each
account. The daemon then makes no new account for a member while the inbox still holds a folder with the account name,
and its warning asks to remove the folder. The volume can start empty, as a new named volume or as a bind mount of an
empty host directory.

Each Samba entry that the daemon creates stores the uid and the join date of its user in its description field
(`smb-sync uid=<uid> joined=<join date>`), and the daemon manages only an entry with that mark, a uid of `UID_BASE` or
more, and the account name of that uid. The unix accounts behind those logins live in the ephemeral filesystem of the
container, and at start the container makes them again from these entries, before `smbd` runs and with no call to
paperless. The first cycle repairs an account that the start could not restore.

The first start stores `UID_BASE` in `smb-sync-uid-base`. A later start with another value stops with a message that
names the way out:

> smb-sync: UID_BASE is 30000, but the Samba volume was made with UID_BASE 20000. Set UID_BASE back to 20000, or start
> with an empty Samba volume and an empty inbox volume.

A start on a volume that holds accounts of smb-sync but not this file, as the volume of an earlier version does, stops
with this message:

> smb-sync: the Samba volume holds accounts of an earlier version of smb-sync, or it lost the file smb-sync-uid-base.
> This version needs an empty Samba volume and an empty inbox volume, and each user then gets a new password.

### Entries of another paperless database

The daemon compares the join date in the mark of an entry with the paperless user of the same id. An entry whose user
has another join date belongs to another person, which happens after a new install of paperless behind the same volumes,
or with a `PAPERLESS_URL` of another paperless. The daemon closes the login of such an entry and takes its line out of
the map, and nothing else of the entry changes. While such an entry exists, or while paperless has none of the users of
the entries, the daemon is on hold: it creates no account and deletes nothing, and where it would remove an account, it
closes the login instead. The way out is to point the daemon to the paperless database of the entries, or to start with
empty volumes.

An export and import with the tools of paperless is no such case. The user ids stay, and the join date counts
milliseconds, which the export keeps.

A third way out fits one case only: one entry alone belongs to another person, and the operator knows that its former
user is gone, for example after paperless gave a user id out a second time. The operator removes that entry by hand with
`docker exec <container> pdbedit -x -u <account name>`, and then removes the inbox folder of that name. The hold ends
when the entry is gone, unless paperless knows none of the entries that are left. Until the folder is gone as well, the
daemon warns that the folder keeps the member from getting an account, and after that it makes a new account for the
member, who gets a new password. This way is wrong when several entries are foreign, because the daemon then points to
another paperless database.

After a move to a paperless with other users, the hold lasts until the operator empties the volumes by hand. When an
administrator deletes the paperless user of the last entry that paperless knows, the daemon closes that login and keeps
it closed, and it makes no account until the volumes are empty.

### Deactivation suspends, it does not delete

Setting a paperless user's account to inactive (without removing them from the group) disables their Samba login but
leaves their inbox folder and workflow in place. Reactivating the user re-enables the account with the same data intact.
This protects against a temporary deactivation being mistaken for an offboarding.

### Removing a user deletes their inbox folder and its contents

Outside a hold (see Entries of another paperless database above), leaving `SYNC_GROUP` or being deleted in paperless
counts as offboarding: the daemon deletes the user's Samba account, their `[smb-sync]` workflow, and their inbox folder,
**including any files still sitting in it**. When each account that paperless still knows is due for removal and an
account of a deleted user is also left, the daemon closes the login of one of the known accounts and removes it one
cycle after the others. Make sure documents are consumed (or moved elsewhere) before removing someone from the group. To
lose only the login and keep the data, deactivate the paperless user instead of removing them from the group.

The daemon deletes an inbox folder only together with the Samba account of its user, and only when the owner of the
folder is the uid that the entry stores. A folder with no account stays in place, with a warning. Each removal runs in
this order: the login closes and the open sessions end, the workflow goes, then the credentials documents, which the
daemon also empties from the trash along with each credentials document that has no owner, then the folder with its
contents, then the unix account, and last the Samba entry. The workflow goes after the login closes, so a scan that
arrives while the login is still open still gets that user as its owner. A failed step ends the removal, and the next
cycle starts it again from the entry.

## Configuration

All configuration is via environment variables, read in `src/smb_sync/config.py`.

| Variable | Default | Meaning |
| --- | --- | --- |
| `CREDENTIALS_DOCUMENT` | `false` | Upload each credentials block to paperless as a document for the user. Needs the token of a superuser. `1`, `true`, `yes`, and `on` turn it on, `0`, `false`, `no`, and `off` turn it off, in any case, and any other value stops the start. |
| `PAPERLESS_API_TOKEN` / `PAPERLESS_API_TOKEN_FILE` | required (one of the two) | API token, given directly or as a path to a file containing it. Setting both is a startup error. |
| `PAPERLESS_CONSUMPTION_DIR` | `/usr/src/paperless/consume` | The consumption directory as the paperless container sees it, which starts the filter of each managed workflow. An absolute path with none of the pattern characters `*`, `?`, `[`, and `]`, and with no `//`, `.`, or `..` part. |
| `PAPERLESS_GID` | `10000` | GID of the shared `paperless` group that owns each inbox folder, 1 or more. After a change, the daemon gives the folders and their files the new group. |
| `PAPERLESS_URL` | required | Base URL of the paperless instance, e.g. `http://webserver:8000`. It has to be the address that paperless answers on without a redirect, since the daemon stops a write that gets one. |
| `POLL_INTERVAL` | `30` | Seconds between reconcile cycles, 1 or more. |
| `SHARE_NAME` | `inbox` | Name of the exported SMB share. |
| `SMB_HOST` | empty | Hostname or IP shown in the credentials block (`smb-sync regen` output and credentials document). It is for display only and does not affect how the share is served. |
| `SYNC_GROUP` | `smb` | Paperless group whose members are provisioned. |
| `UID_BASE` | `20000` | Base for the unix UID of managed accounts, 1000 or more. A user's UID is `UID_BASE` plus their paperless user id. Do not change it after the first start: the Samba volume stores the value of its first start, and the start refuses another value. |

The API token needs permission to read users and groups and to read, create, update, and delete workflows. Only the
credentials document needs the token of a superuser, though a superuser's token is the simplest choice either way.

## Custom Samba configuration

The container generates `/etc/samba/smb.conf` on each start, so edits to it do not survive a restart. To add your own
Samba settings, mount one or more `*.conf` files into `/etc/samba/conf.d/`:

```yaml
    volumes:
      - ./smb-custom.conf:/etc/samba/conf.d/custom.conf:ro
```

Drop-ins are appended to the generated file in filename order, after the managed share. A `[global]` header in a drop-in
therefore re-opens the global section and its settings override the defaults, and any other section header defines an
additional share. Only a login name of the username map can log in, to such a share as well, so a Samba entry that you
add by hand opens no share. The container runs `testparm` over the merged file at startup and refuses to start if
`testparm` fails or reports an unknown parameter, so a misspelled setting shows up immediately in the log instead of
being ignored for the life of the container.

A drop-in that repeats the managed share's section name (`inbox`, or whatever `SHARE_NAME` is set to) merges into it
rather than replacing it, so that is the supported way to add settings to the share itself. Do not override its
`force group`, `path`, or `valid users` there, and do not override `username map` in a `[global]` section. `force group`
gives each file the group of paperless, `path` routes each account into its own inbox folder, `username map` turns each
login name into its account, and `valid users` admits the members of the group `smb-sync`, so changing any of them
breaks the share for all of its users.

### Making the inbox write-once

Anyone holding a user's SMB credentials can overwrite or delete that user's scans while they wait to be consumed.
Samba's `worm` module closes that off by turning a file read-only once it is older than a grace period:

```yaml
[inbox]
    vfs objects = worm
    worm:grace_period = 300
```

New files can still be created, so scanning keeps working, and paperless reaches the folder through the filesystem
rather than over SMB, so consumption and cleanup are unaffected. Only access arriving over SMB is restricted.

Keep the grace period long enough for a scan job to finish. Scanners that write to a temporary name and rename it, or
that reopen a file to append pages, fail against a file that has already frozen. Five minutes is a safe starting point,
and `0` is not.

## Setup

The image is published as `ghcr.io/flobernd/paperless-smb-sync` for amd64 and arm64, with these tags:

- `X.Y.Z` and `X.Y` for a release, and `X` as well from 1.0 on
- `latest` for the most recent release without a suffix
- `edge` for `main`

A release with a suffix, such as `1.0.0-rc.1`, gets its version tag alone. A weekly run rebuilds the latest release and
`main` with the current Debian updates and moves their tags to the new images, so a version tag can name a newer build
of the same source. A digest pins one build. GitHub turns the weekly run off after 60 days without activity in the
repository, until a maintainer turns it on again, so an image can be older than a week.

1. In paperless, create the `smb` group (or whatever name you plan to use for `SYNC_GROUP`).
2. Create an API token in paperless (user profile settings) for the container to use as `PAPERLESS_API_TOKEN`, from a
   superuser if you plan to enable `CREDENTIALS_DOCUMENT`.
3. Copy `compose.example.yaml` to `compose.yaml`, set `PAPERLESS_URL` and `PAPERLESS_API_TOKEN` (or mount a secrets file
   and use `PAPERLESS_API_TOKEN_FILE` instead), and point the `/shares/inbox` volume at the same host directory the
   paperless consumer watches.
4. Set `PAPERLESS_CONSUMPTION_DIR` if paperless sees that directory at a path other than `/usr/src/paperless/consume`,
   and give paperless the group id `PAPERLESS_GID` (with `USERMAP_GID` in the official image).
5. Run paperless with `PAPERLESS_CONSUMER_RECURSIVE=true`, because without it paperless ignores files in subdirectories
   of its consumption directory, such as the inbox folders.
6. Start it: `docker compose up -d`.
7. Add users to the `smb` group in paperless.
8. Once the next cycle has made the accounts, run `docker exec <container> smb-sync regen <login name>` for each user
   and enter the printed server, share path, username, and password into the scanner's SMB profile.

With `CREDENTIALS_DOCUMENT` enabled the block reaches the user as a document in paperless instead, so the last step can
be theirs rather than yours. Each user is provisioned within one `POLL_INTERVAL` of joining the group, and the login
opens one cycle later.

To build the image from a checkout, uncomment `build: .` in `compose.yaml` and start it with
`docker compose up -d --build`.

### Upgrading from 0.1.0

Version 0.2.0 needs an empty Samba volume and an inbox volume without the inbox folders of 0.1.0, and each user gets a
new password. On the Samba volume of 0.1.0 the start stops with the message about an earlier version, as described under
The Samba volume holds the state above.

Stop the container first, which ends the logins of 0.1.0, so no scan arrives after you checked the folders. Paperless
still reads the inbox folders, and the workflows of 0.1.0 still give each waiting scan its owner, so let paperless
consume those scans. Check that paperless consumed them and that each inbox folder is empty. Delete the credentials
documents of 0.1.0 in paperless by hand, since the daemon takes such a document for its own, and `regen` can then write
a new password into the old document instead of the new one. Then empty the Samba volume, and remove the inbox folders
of 0.1.0 from the inbox volume, which is also the consumption directory of paperless. A folder of 0.1.0 that stays gives
a warning and stops no account. Only a folder with the name of an account, such as `u20005`, stops the creation of that
account. In its first cycle the daemon removes the workflows of 0.1.0 by itself.

A share of a 0.1.0 drop-in that names `@paperless` in `valid users` or `%U` in its path needs a change: the accounts are
members of the group `smb-sync` and no longer of `paperless`, so `@paperless` admits none of them, and `%U` is now the
login name, while an inbox folder has the account name, which is `%u`.

## Admin commands

Run these against the running container with `docker exec <container> smb-sync <command>`.

**`regen <login name>`** sets a new random password for a managed account and prints the one-time credentials block to
the terminal. It finds the account through the username map without case, and it holds the cycle lock until it is done,
so no cycle closes or removes the account in the meantime. With `CREDENTIALS_DOCUMENT` enabled it also adds the block to
the user's credentials document, or uploads a new one when the user has none. For a name without an account it fails
with one line, `no managed account for the login name '<name>'`.

**`status`** lists one row for each member of `SYNC_GROUP` and each managed Samba entry, with the login name, the
account name, the uid, the login (`closed`, `missing`, `no login name`, `open`, or `pending removal`), the inbox folder,
and the workflow. The warnings of that cycle follow the table.

**`sync`** runs one reconcile cycle immediately, instead of waiting for the next `POLL_INTERVAL`, and logs each of its
warnings.

The subcommands `check`, `health`, `restore`, `run`, and `volume` exist for the entrypoint and the health check.

## License

Released under the MIT license. See [`LICENSE`](LICENSE) for the full text.
