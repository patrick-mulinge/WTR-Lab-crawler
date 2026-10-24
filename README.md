# Library cleanup (Eternal)

A user named **Eternal** requested disk cleanup after successful deliveries:

- Delete the **EPUB** immediately after it is successfully sent to Telegram (already the case).
- **24 hours** after a **fully successful** download (EPUB sent and task marked done), delete **only that novel’s** folder under `data/library/`.
- If the download **fails or is incomplete** (including AI-locked later chapters), **keep** chapter files so `/continue` can resume.
- Do **not** delete files belonging to other active or incomplete novels.

The 24h schedule is stored in SQLite (`library_cleanup`) and **survives bot restarts**. Re-downloading the same novel cancels a pending delete for that folder.

This branch is a variant of the Linux/seleniumbase worker for deployments that want this cleanup policy.
