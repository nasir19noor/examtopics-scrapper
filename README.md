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

Paste a discussion URL into the form, click **Scrape**, then **Copy** or
**Download** the `.txt`. Tick **Include header** to prepend the exam name /
Question # / Topic #.

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

| File                 | Purpose                                        |
|----------------------|------------------------------------------------|
| `webapp.py`          | Flask browser UI (served by gunicorn in Docker)|
| `scraper.py`         | Scraping engine + standalone CLI               |
| `Dockerfile`         | Builds the web-UI image                         |
| `docker-compose.yml` | Runs it on port 8000                            |

## Notes

- Extraction targets the page's `question-body` and `question-choices-container`
  elements. If ExamTopics changes its HTML, update the selectors in
  `scraper.parse()`.
- The download happens client-side from the textarea — no files are written on
  the server in web mode.
