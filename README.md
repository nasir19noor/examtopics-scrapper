# examtopics-scrapper

Extracts the **question text and answer choices** from an ExamTopics discussion
page. Use it from the **browser** (paste a URL) or the **command line**.

It captures exactly the block shown *after* the `[All ... Questions]` header
line and *before* the `by <author> at <date>` line — the question and its
A/B/C/D options, with no page header, disclaimers, or community comments.

## Run with Docker (browser UI)

```bash
docker compose up --build      # then open http://localhost:8000
```

The UI has three tabs:

- **Single URL** — paste a discussion URL, click **Scrape**, then **Copy** or
  **Download** the `.txt`. Tick **Include header** to prepend the exam name /
  Question # / Topic #.
- **Bulk by question #** — paste *any one* URL from an exam plus a range like
  `1-117`, click **Collect**, and download a zip of `1.txt`, `2.txt`, …

  An exam's questions are spread over several ranges of discussion ids (see
  [Id batches](#id-batches)). Give it one URL per batch — the first goes in the
  top field, and **+ Add another batch URL** adds a field for each of the rest,
  as many as you need. With every batch seeded the hunt is skipped and the run
  is much faster. Leave them blank and tick **Hunt other id bands** to let it
  find the batches itself instead.
- **Examcademy** — scrape examcademy.com instead. It gives the correct answer
  and an explanation per question, but only exposes questions 1-25 without a
  login. See [examcademy.com](#examcademycom).

Without compose:

```bash
docker build -t examtopics-scrapper .
docker run --rm -p 8000:8000 examtopics-scrapper
```

> First time only: if Docker gives a socket permission error inside WSL, run
> `sudo usermod -aG docker $USER`, then restart the shell (`wsl --shutdown`).

## Run the web app without Docker

```bash
pip install -r requirements.txt
python webapp.py               # http://localhost:8000
```

## Command-line use (no browser)

The scraper engine also works standalone:

```bash
python scraper.py "https://www.examtopics.com/discussions/amazon/view/95548-...-sap-c02/"
python scraper.py "<url>" --header -o output
```

| Option       | Default  | Purpose                                      |
|--------------|----------|----------------------------------------------|
| `-o, --out`  | `output` | Output directory.                            |
| `--header`   | off      | Prepend exam name / Question # / Topic #.    |
| `--delay`    | `2.0`    | Seconds between requests (multiple URLs).    |

## Bulk: every question of one exam

Give it **one** discussion URL from the exam and a range of question numbers.
It writes `1.txt`, `2.txt`, … one file per question:

```bash
python finder.py "https://www.examtopics.com/discussions/amazon/view/384242-exam-aws-certified-generative-ai-developer-professional-aip/" \
  -n 1-117 -o output --header
```

| Option          | Default  | Purpose                                            |
|-----------------|----------|----------------------------------------------------|
| `-n, --numbers` | `1-100`  | Question numbers: `1-117`, or `1,3,5-10`.          |
| `-o, --out`     | `output` | Where the `N.txt` files go.                        |
| `--header`      | off      | Prepend exam name / Question # / Topic #.          |
| `--window`      | `400`    | Ids swept densely either side of each seed.        |
| `--seed URL`    | –        | Extra seed from another batch; repeatable. See below. |
| `--reach`       | `30000`  | How far the coarse hunt looks. `0` disables it.    |
| `--coarse-step` | `6`      | Coarse stride; must be under the narrowest batch.  |
| `--workers`     | `8`      | Concurrent probes.                                 |

### How it finds the pages

No search engine is involved — deliberately. Scraping Google returns a consent
wall to scripted clients, and DuckDuckGo is DNS-blocked by some ISPs, so
neither is dependable. Instead this uses two properties of ExamTopics itself:

1. **The slug is ignored on input.** `view/384242-x/` resolves fine, and the
   server answers `301` with the *real* slug in the `Location` header.
2. **That redirect is served to `HEAD` with an empty body.**

So the exam a numeric id belongs to can be identified with one tiny bodiless
request, and only ids whose slug contains the seed's key — e.g.
`exam-aws-certified-generative-ai-developer-professional-aip` — get downloaded
in full.

Matching on the slug is also what keeps the results *correct*. A keyword search
happily returns a near-identical exam: searching AIP-C01 question text can land
you on **AIF-C01** ("AWS Certified AI Practitioner"), a completely different
exam. The slug is unambiguous.

### Id batches

An exam's pages are **not one contiguous run of ids**. Questions get added in
batches over time, and each batch lands wherever the site's global id counter
happens to be, so one exam occupies several narrow bands scattered tens of
thousands of ids apart. For AIP-C01:

| Questions | Discussion ids  | Width | Order      |
|-----------|-----------------|-------|------------|
| 1 – 85    | 384102 – 384293 | 192   | shuffled   |
| 86 – 97   | 402483 – 402494 | 12    | sequential |
| 98 – 117  | 421752 – 421771 | 20    | sequential |

Note the gaps: ~18,200 ids between the first and second batch, ~19,300 between
the second and third. A window sweep around a single seed can never reach them.

Hence two phases. **Dense**: sweep every id within `--window` of each seed —
cheap, and enough when the numbers you want are in the seed's own batch.
**Coarse**: if numbers are still missing, probe every `--coarse-step` ids
outward as far as `--reach`; a batch is dense once you are inside it, so a
stride narrower than the smallest band (12 above) reliably detects one, and any
hit gets filled in densely. Both phases stop the moment every requested number
is found.

The coarse hunt does find the far batches unaided — it located
421752 – 421771 in about 6 minutes and ~7,000 probes. But if you already know a
URL from each batch, pass them and skip the hunt entirely:

```bash
python finder.py "<url from batch 1>" \
  --seed "<url from batch 2>" --seed "<url from batch 3>" \
  -n 1-117 --reach 0 -o output --header
```

That collects all 117 questions in about 90 seconds.

If numbers still come back missing, they either have no discussion page yet
(the exam page can list more questions than the community has threads for) or
they sit in a batch beyond `--reach`.

## Output example

Saved/downloaded as `SAP-C02_Q135.txt`:

```
A company is creating a sequel for a popular online game. ...

• Amazon S3 bucket that stores game assets
• Amazon DynamoDB table that stores player scores

A solutions architect needs to design a multi-Region solution ...

What should the solutions architect do to meet these requirements?

A. Create an Amazon CloudFront distribution ...
B. Create an Amazon CloudFront distribution ...
C. Create another S3 bucket in a new Region ...
D. Create another S3 bucket in the sine Region ...
```

## Files

| File                 | Purpose                                          |
|----------------------|--------------------------------------------------|
| `webapp.py`          | Flask browser UI (served by gunicorn in Docker)  |
| `scraper.py`         | Scraping engine (one page) + standalone CLI      |
| `finder.py`          | Locates every page of an exam by slug + bulk CLI |
| `examcademy.py`      | Scrapes examcademy.com + standalone CLI          |
| `Dockerfile`         | Builds the web-UI image                          |
| `docker-compose.yml` | Runs it on port 8000                             |

## examcademy.com

A second source, with a different trade-off: it ships the **correct answer and
an explanation** alongside each question, which the ExamTopics discussion pages
don't. Same output shape — one `N.txt` per question.

```bash
python examcademy.py "https://examcademy.com/exams/amazon/aws-certified-generative-ai-developer-professional-aip-c01/1" \
  -n 1-25 -o output --header
```

| Option          | Default  | Purpose                                          |
|-----------------|----------|--------------------------------------------------|
| `-n, --numbers` | `1-25`   | Question numbers: `1-25`, or `1,3,5-10`.         |
| `-o, --out`     | `output` | Where the `N.txt` files go.                      |
| `--header`      | off      | Prepend exam name / Question #.                  |
| `--no-answers`  | off      | Omit the correct answer and explanation.         |
| `--cookie`      | –        | `Cookie` header from a signed-in session.        |

### Reaching the gated pages

**Only questions 1-25 are public.** The site paginates 25 to a page, and pages
2+ render the app's error component for anonymous requests — so a 117-question
exam yields 25 questions and a note about the other four pages.

Examcademy signs in through Auth0 and offers email/password, **Google** and
Microsoft. This tool deliberately does **not** drive that flow: automating a
Google sign-in violates Google's automation policies and breaks on device
checks, 2FA and CAPTCHA. Log in yourself, then lend the tool your session:

1. Sign in at examcademy.com in your browser (Google is fine).
2. DevTools → **Network** → click any document request to `examcademy.com`.
3. Copy the whole **`Cookie`** request header — from `examcademy.com`, *not*
   `auth.examcademy.com`.
4. Pass it with `--cookie "…"`, or paste it into the Examcademy tab.

**Save it once in `.env`.** Copy `.env.example` to `.env` and set
`EXAMCADEMY_COOKIE` — the CLI and the web UI both use it automatically whenever
you don't pass a cookie explicitly, so you paste it once instead of on every
run. `.env` is gitignored.

```bash
cp .env.example .env
# edit .env:  EXAMCADEMY_COOKIE="<the whole Cookie header>"
python examcademy.py "<exam url>" --check-auth      # cookie source: .env
```

> Why not just log in with the credentials? examcademy's Auth0 login has bot
> protection that loops scripted attempts, and Chrome 151 seals its cookie
> store with app-bound encryption — so neither automated login nor reading the
> browser's cookies is workable. Borrowing the session cookie is the reliable
> path, and this tool will not try to defeat either protection.

Check it landed before doing a long run:

```bash
python examcademy.py "<exam url>" --check-auth --cookie "<cookie header>"
# session: authenticated
# Cookie accepted - gated pages should be reachable.
```

The **Check sign-in** button in the Examcademy tab does the same thing. If a
page is still gated the tool says which case it hit — no cookie, a cookie the
site rejected, or a signed-in account that lacks access to that exam.

### How it reads the page

Examcademy is a Next.js app, so the served HTML is mostly skeleton — selectors
against it come back empty. The real content is in the RSC "flight" payload,
streamed as `self.__next_f.push([1,"…"])` calls. Reassembling those yields one
self-contained MDX chunk per question:

```
<stem markdown>

<MCQuestion
  choices={{"A":"…","B":"…"}}
  correctAnswer={"B"}
  explanation={"…"}
/>
```

`"questionNumber":N,"mdxContent":"$<id>"` maps question numbers onto those
chunks. Parsing the payload rather than the DOM is also why this needs no
headless browser.

## Tuning fetch behaviour

A normal scrape takes ~2s. If it ever takes much longer, ExamTopics (behind
Cloudflare) is throttling or stalling — these environment variables bound how
long the app is willing to wait:

| Variable                  | Default | Purpose                                          |
|---------------------------|---------|--------------------------------------------------|
| `SCRAPER_TOTAL_TIMEOUT`   | `25`    | Wall-clock budget for a fetch, **all retries included**. This is the hard cap on how long "Scrape" can hang. |
| `SCRAPER_CONNECT_TIMEOUT` | `10`    | Per-attempt connect timeout.                     |
| `SCRAPER_READ_TIMEOUT`    | `20`    | Per-attempt read timeout.                        |
| `SCRAPER_MAX_ATTEMPTS`    | `3`     | Attempts per fetch (retries `5xx` only).         |
| `SCRAPER_CACHE_TTL`       | `300`   | Seconds a fetched page stays cached; `0` disables. |
| `SCRAPER_FORCE_IPV4`      | `1`     | Resolve A records only — Docker's bridge network usually can't route IPv6, and waiting for that to time out is slow. |
| `BULK_WINDOW`             | `400`   | Ids swept densely either side of each seed in bulk mode. |
| `BULK_REACH`              | `30000` | How far the coarse band hunt looks, when enabled. |
| `BULK_WORKERS`            | `8`     | Concurrent probes in bulk mode. Raising this makes 429s more likely. |
| `BULK_JOB_MAX`            | `8`     | Bulk jobs kept in memory before the oldest is dropped. |

Two behaviours worth knowing:

- **HTTP 429 fails immediately** rather than sleeping out the `Retry-After`.
  ExamTopics sends `Retry-After: 60`, and honouring it across retries meant a
  single click could block for ~3 minutes. You now get an instant "try again in
  ~60s" message.
- **Repeat scrapes of the same URL are served from an in-process cache**, so
  they return in milliseconds instead of a fresh round trip.

If you hit rate limiting often, slow down: each click is a request, and
double-clicking **Scrape** is the quickest way to get throttled.

> Bulk jobs are tracked in process memory, so gunicorn must run a **single
> worker** with threads (as the `Dockerfile` does). With 2+ workers a progress
> poll can land on a worker that doesn't know the job and 404.

## Notes

- Extraction targets the page's `question-body` and `question-choices-container`
  elements. If ExamTopics changes its HTML, update the selectors in
  `scraper.parse()`.
- The download happens client-side from the textarea — no files are written on
  the server in web mode.
