# Delivery to an existing Music directory

The optional `python -m phonoteka.music_sync` process delivers only settled, accepted Ready publications whose receipts match committed history. It needs separately prepared configuration and a delivery ledger; starting the application does not enable it. Existing paths remain stable. Conflicting external edits are retained for review. Atomic replacement preserves the displaced revision and access metadata, including Drive index hard links. The process does not approve proposals or lyrics.

## Directory identity

New `music_identity` values use an object with `kind: "statvfs-fsid-inode-v1"`, a nonzero filesystem ID as 16 lowercase hexadecimal characters, and a positive directory inode as a decimal string. The ID is read from an opened directory descriptor using `fstatvfs`; every path component is opened without following symbolic links. The configured identity must exactly match the ledger and mounted directory. Unknown identities or unavailable filesystem IDs stop delivery.

Legacy `[device_number, inode]` values retain their original strict comparison. They are never upgraded or rebound automatically. Linux can assign a different device number after a restart even when the filesystem and directory are unchanged. On Btrfs, the filesystem ID incorporates the filesystem UUID and subvolume ID: see [Linux Btrfs statfs](https://github.com/torvalds/linux/blob/v4.4/fs/btrfs/super.c) and [Python statvfs](https://docs.python.org/3.13/library/os.html#os.statvfs). This identity guard supplements the per-file SHA-256 and concurrency checks; it does not replace them or authenticate cloned filesystems.

## Recovery from a legacy root mismatch

1. Stop only this delivery service and ensure no file transaction is pending. Preserve protected copies of its Compose, configuration and ledger.
2. Independently verify the actual mount, directory, filesystem and complete manifest of previously delivered files. Every expected file hash must match. An unavailable or mismatched file blocks rebinding; never overwrite it to make the audit pass.
3. With no intervening changes, explicitly set the same persistent identity in both configuration and ledger, preserving every other field. Keep rollback copies and before/after hashes. This is an operator migration, not a startup side effect.
4. Start the compatible service, check its health and verify a real accepted publication through Ready, Music and the Drive server. Phone-client synchronization is separate acceptance.

Rollback before another delivery may restore the exact protected configuration and ledger. After new deliveries, retain the new ledger and file history; restoring an old ledger would discard receipts. Stop this service and reconcile forward instead.

Tests cover both identity formats, changed filesystem/directory IDs, renumbered device IDs, missing identity support, symbolic links, unaccepted publications, external edits, an exchange race and process restart after commit. They do not claim physical NAS reboot or phone acceptance.
