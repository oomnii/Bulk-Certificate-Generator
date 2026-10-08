# Interview guide

This note explains how the project actually works. It is meant to be read beside the code. It does not describe features that were not built.

## 1. Request to PDF

1. `POST /api/v1/jobs` hits `create_certificate_job` in `app/api/jobs.py`.
2. Pydantic checks that the body is an object with a course name, an issuer, a date, and a non-empty recipients array. Recipient items are `Any`, not a strict nested model.
3. `create_job` in `app/services/job_service.py` checks the recipient count and the trimmed course name and issuer against settings. Those failures raise `422` and write nothing.
4. Each recipient is validated in `app/services/validation.py`. Valid rows are `pending`. Invalid rows are `failed` with `error_message` and `processed_at` set.
5. The job and all rows are inserted and committed together. The response counts are read back from those rows. HTTP status is 202.
6. `python -m app.workers.certificate_worker` polls. `claim_next_recipient` selects the oldest pending row and updates it to `processing` only if it is still `pending`.
7. The worker commits that claim, then calls `generate_certificate_file`. The PDF is rendered to a temp file and `os.replace`d to `storage/<job_id>/<recipient_id>.pdf`.
8. A new database session marks the row `completed` and stores the relative path, or `failed` with `Certificate generation failed`.
9. `refresh_job_status` sets the job status from the recipient counts.
10. The client polls `GET /api/v1/jobs/{job_id}`, lists `/certificates`, and downloads by recipient id.

The API process never generates the PDF. If the worker is not running, valid rows stay `pending` and the job stays open.

## 2. Important files

| File | Responsibility |
| --- | --- |
| `app/main.py` | Builds the app, creates tables on startup, serves `/health`. |
| `app/config.py` | Settings and defaults. |
| `app/database.py` | Engine, `session_scope`, foreign keys, WAL, busy timeout. |
| `app/models.py` | Two tables and the status strings. |
| `app/schemas.py` | Public JSON shapes. |
| `app/api/jobs.py` | Create, poll, list, ZIP. |
| `app/api/certificates.py` | One-file download. |
| `app/services/validation.py` | Rules that differ between the whole request and one person. |
| `app/services/status.py` | Pure functions for status and progress. |
| `app/services/job_service.py` | Transactions, claim, recovery, response building. |
| `app/services/pdf_renderer.py` | The only template. |
| `app/services/storage.py` | Path checks and atomic rename. |
| `app/workers/certificate_worker.py` | Loop, signals, one-recipient error handling. |

## 3. Database relationships

`certificate_jobs` has many `certificate_recipients`. `certificate_recipients.job_id` references `certificate_jobs.id` with `ON DELETE CASCADE`. SQLite only enforces that because each connection runs `PRAGMA foreign_keys=ON`.

The pair `(job_id, input_index)` is unique, so the original order can be stored even when names are duplicated or missing. Indexes on `status` and `(job_id, status)` match the worker's claim query and the count query.

Generated PDF bytes are not a column. `certificate_path` is a relative path such as `2f1c.../9ab0....pdf`.

`processing_started_at` and `attempt_count` are not part of the public status model. They exist so a crashed claim can be detected and limited.

## 4. How validation is implemented

If recipients were a `list[Recipient]` Pydantic model, one bad email would fail the entire request with 422. The assignment needs the opposite: save the bad row and continue.

So the outer model only requires `recipients: list[Any]` and rejects an empty list. `validate_recipient` then inspects each item:

- non-objects become a failed row with no name and no email
- name and email are checked independently
- both errors can be returned, joined by `; `
- only surrounding whitespace is stripped

Job fields are different. A blank course name means there is no certificate to generate, so the request is 422 and nothing is stored.

## 5. How background processing works

`serve` loops until Ctrl+C or `SIGTERM` sets a stop event.

- If `process_next_recipient` does work, it loops again immediately.
- If there is nothing pending, it sleeps for `WORKER_POLL_INTERVAL_SECONDS`.
- The PDF is generated after the claim transaction commits, so a slow render does not hold a write transaction open for the whole draw.
- Each claim, failure update, and success update uses its own `session_scope`. A failed commit rolls that session back and the next recipient gets a new session.

The supported command is:

```text
python -m app.workers.certificate_worker
```

Run it from the repository root so `DATABASE_URL` and `STORAGE_DIR` resolve to the same files the API uses.

## 6. How one failure stays isolated

Validation: the bad recipient is a `failed` row. The insert of the other rows is in the same transaction, so either the whole job is stored or, on a database error, none of it is.

Generation: `process_next_recipient` has its own `try` around `generate_certificate_file`. On exception it marks that recipient failed and returns. The loop claims the next pending row. A fixed client message is stored. The traceback is logged with the recipient id.

A failure while saving the success row deletes the new PDF and leaves the row `processing`. It does not mark a downloadable success when the database update did not commit, and it does not stop the following recipient.

## 7. How PDFs are generated

`render_certificate_pdf` registers DejaVu Sans and DejaVu Sans Bold from `assets/fonts/` once per process. No font is downloaded at runtime.

The page is landscape A4. The name, course, and issuer are wrapped to the content width. If a name still needs too many lines, the font size steps down. Very long tokens are split by character width so a 200-character name cannot overflow the page.

ReportLab paragraph markup is not used. User text is passed to `drawCentredString`, so `<` and `&` are just characters.

`write_pdf_atomic` creates the temp file in the destination directory (same filesystem), checks that the result is non-empty and starts with `%PDF`, then `os.replace`s it. A crash or exception deletes the temp file. Clients never read a half-written PDF through the final name.

## 8. How counters are calculated

`count_recipients` groups recipient rows by status. `derive_job_status` and `progress_percent` are pure functions in `app/services/status.py`.

```text
progress_percent = 0 when total is 0
otherwise round((completed + failed) * 100 / total, 2)
```

Status rules:

- `processing` if any row is `processing`, or if some are `pending` and some are already `completed`
- `pending` if someone is still `pending` and nobody has completed yet
- otherwise `completed`, `completed_with_errors`, or `failed`

`GET /api/v1/jobs/{job_id}` uses those functions on the current rows. It does not trust a separately incremented integer that could drift.

## 9. How certificates are retrieved

Download looks up `CertificateRecipient` by primary key. The id must be a UUID, so `../` is not a valid id and FastAPI returns 422.

If the row is not `completed`, the response is 409. The relative path is joined to `STORAGE_DIR`, resolved, and rejected unless it stays inside that directory and ends in `.pdf`. Missing files are 404. The response `Content-Type` is `application/pdf`.

The ZIP route loads completed rows in input order, skips missing files, and writes a temp ZIP. Entry names include `input_index` and the recipient UUID.

## 10. How crashes are handled

The claim commits `processing`, `processing_started_at`, and `attempt_count` before rendering. If the process dies during ReportLab:

- the final PDF name was not published unless `os.replace` already ran
- the temp file is not a completed row
- the next worker start calls `recover_stale_recipients`
- a row still inside the lease is left alone, in case the original worker is alive
- an expired row with `attempt_count` below the limit returns to `pending`
- an expired row that has already used `MAX_GENERATION_ATTEMPTS` becomes `failed` with `Certificate generation was interrupted too many times`

Completed rows are not selected by the claim query, so a restart does not generate a second file for them. Giving up on a row also deletes `storage/<job_id>/<recipient_id>.pdf` if a crash had already renamed that file into place. A caught generation error does the same, so a failed row does not leave a certificate on disk.

This is not indefinite retry. A generation exception that is caught is a terminal `failed`, not a crash, and it is not retried.

## 11. Architectural tradeoffs

- A database poll is easier to explain than Celery, and it is enough for hundreds of certificates on one machine. It wakes up at a fixed interval and it is not a fair multi-consumer queue.
- SQLite makes the project runnable with one command. It allows one writer at a time. WAL and a busy timeout make the API and the single worker coexist. They do not make SQLite a job bus.
- Generating outside the HTTP request adds a worker to operate. It also makes status, failure isolation, and restarts straightforward.
- Storing files on the local disk is simple and wrong for several API replicas. The path rules are still the right boundary if the disk later becomes a bucket.
- No authentication keeps the assignment small and means the service must not be exposed as-is.

## 12. Scaling, without pretending it is done

For a much larger batch the same schema still works, but the worker should stop polling SQLite. PostgreSQL `FOR UPDATE SKIP LOCKED`, or a real queue, lets several workers claim different rows. PDFs should go to object storage. The API should keep accepting the job in one request, or accept a file upload, and should enforce a tighter rate limit. Counting with SQL `GROUP BY` is still fine well past the current 500-recipient cap. The cap exists to protect this local design, not because the table cannot hold more rows.

## Short answers

### Why FastAPI?

The assignment needs a small JSON API with validation and OpenAPI. FastAPI provides that without the admin and ORM coupling of a larger framework. The domain code is ordinary Python and SQLAlchemy, which is what an interview discussion should be about.

### Why background processing?

PDF work is slower than request validation, and one recipient must be able to fail alone. The request's job is to record the batch and return an id.

### Why not generate every PDF inside POST?

The client would wait for the whole batch. A timeout or a crash would hide work that had already succeeded, and a bug in one name could fail the HTTP call instead of one row. The status endpoints would also be less meaningful if the job was finished before the response.

### Why a relational database?

Jobs and recipients are a one-to-many relationship with a status lifecycle, ordering, and counts. SQL transactions make "save every recipient or none of this request" easy to state. An in-memory dict would vanish on restart and would not match the requirement.

### What if one recipient is invalid?

The job is still created. That input index is stored as `failed` with a message such as `Name is required; Email is not valid`. Other people stay `pending`. The create response already shows `failed` greater than zero and a progress percentage that includes those failures.

### What if PDF generation fails?

That recipient becomes `failed` with `Certificate generation failed`. No final PDF is left for them. The worker claims the next pending recipient. If at least one PDF succeeded, the finished job is `completed_with_errors`. If every recipient failed, it is `failed`.

### What if the server restarts?

SQLite still has the rows. Pending recipients are claimed by the next worker start. A recipient stuck in `processing` past the lease is requeued or failed after too many attempts. Completed files on disk stay downloadable because the path is in the database. A PDF that was renamed but whose success row never committed is deleted on the failed save, or overwritten on the retry.

### How would you handle 100,000 recipients?

Do not do it as one SQLite poller and one local folder. I would accept the batch, insert recipients in chunks, and have several workers claim with `SKIP LOCKED` on PostgreSQL or pull from a queue. I would cap memory by generating one PDF at a time, store objects in S3 or similar, and put a higher limit and backpressure on the API. The per-recipient failure model can stay.

### How would you move from SQLite to PostgreSQL?

Install a driver such as `psycopg`, set `DATABASE_URL=postgresql+psycopg://...`, and run the app. The models use portable types (`Uuid`, `DateTime(timezone=True)`, string statuses). I would then replace `create_all` with migrations and turn the claim into `FOR UPDATE SKIP LOCKED` if I wanted more than one worker. I would test the timezone round-trip, because SQLite currently returns naive UTC datetimes and the recovery code normalizes that.

### How would you add more certificate templates?

I would not grow `if/else` drawing code inside the route. I would add a `template` value on the job, defaulting to the current layout, and a small registry of render functions. The worker would keep calling one function, `render(context)`. The assignment deliberately has one template, so that registry is not in the code.

### How would you add retries?

Crashes already retry until `MAX_GENERATION_ATTEMPTS`. For a caught generation error I would add a `failed` versus `retryable` decision and a delay, still on the same row, rather than introduce Celery just for that. I would keep the client message stable and put the attempt count in logs.

### How would you secure downloads?

The ids are unguessable UUIDs, but that is not access control. I would require an authenticated caller, authorize that caller for the job, use short-lived signed URLs for the file store, and stop serving files from the API process's disk. I would also add rate limits and a retention delete. None of that is implemented.

### What would change for multiple worker processes?

The conditional update stops two workers from marking the same pending row completed. On SQLite they would still block each other on the database lock, and the lease logic assumes a crashed worker rather than a slow peer. I would move to PostgreSQL, claim with `SKIP LOCKED`, and keep the rule that rendering happens after the claim commits. I would not call the current code multi-worker safe just because the `WHERE status = 'pending'` guard exists.

### How do you test this application?

`pytest` from the repository root. Each test builds a `Settings` object pointed at a temporary database and storage directory and starts the app with `TestClient`. PDF tests read the real files with `pypdf` and check the recipient name, course, issuer, and date. Failure tests replace `generate_certificate_file` or the completion write for one call. Recovery tests set `processing_started_at` in the past and run the worker function again. One test starts `python -m app.workers.certificate_worker` as a subprocess against the test database. `ruff check .` and `ruff format --check .` cover lint and formatting.
