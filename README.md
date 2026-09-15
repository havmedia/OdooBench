# OdooBench

Measure an Odoo the way its own web client uses it.

OdooBench logs in as a real user, then sends the calls a browser sends when someone
opens a list, pages through it, filters it or groups it. It reports how many of
those a server gets through, how long they took, and whether a change you made
actually moved anything. Every scenario also states what it *cannot* see, because
that is where most Odoo benchmarks go wrong.

Built on [Locust](https://locust.io). Python 3.9 and up.

## Why this exists

We spent a week benchmarking a 16-core Odoo with 84.5 million records and got a
clear result: tuning PostgreSQL changed nothing. Repeated runs, alternating,
carefully measured. Nothing.

The result was real and the conclusion was wrong. Our load generator sorted by a
column with no index. That leaves the database exactly one possible plan, so no
setting it has can change the outcome. It was a benchmark of the benchmark.

Changing one thing, sorting by the model's own default order the way the web
client does, produced this on the same machine, same data, same hour:

| | requests/s | slowest 5% |
| --- | --- | --- |
| Nothing tuned | 59.9 | 0.95 s |
| Odoo tuned | 158.5 | 0.22 s |
| Database tuned as well | 508.2 | 0.13 s |

OdooBench is the benchmark we wish we had started with, plus the habits we needed
to trust its numbers.

## Install

```
pip install git+https://github.com/havmedia/OdooBench
```

This installs Locust with it.

## Use

```
odoobench scenarios

odoobench run --url http://10.0.0.5:8069 --db production --login admin \
            --scenario office-day --users 50 --out before.json

# change one thing, and only one thing

odoobench run --url http://10.0.0.5:8069 --db production --login admin \
            --scenario office-day --users 50 --out after.json

odoobench compare before.json after.json
```

`odoobench run` starts Locust without its web interface, runs the scenario
several times and writes a result file. The password comes from `--password` or
the `ODOOBENCH_PASSWORD` environment variable. Point it at a copy of production, not at production.

`odoobench run` first asks the instance for the date range its records actually
cover, so the filters it generates hit real data instead of an empty window.

## Two ways to run it

Both use the same scenarios and the same Locust user class, so they generate the
same load. They answer different questions.

**Explore with Locust's web interface.** Ramp users up until the server bends,
watch the chart, keep it running while somebody changes something:

```
locust -f locustfile.py --host http://10.0.0.5:8069 \
       --odoo-db production --odoo-password secret --scenario office-day
```

Then open http://localhost:8089. Distributed mode, ramp-up shapes and everything
else Locust offers work as usual.

**Conclude with `odoobench run`.** Locust reports one number per test. It cannot
tell you whether a difference between two of its runs is real, because it has
nothing to compare the difference with. `odoobench run` repeats each
configuration, keeps the runs apart, and `odoobench compare` refuses to call a
difference real when the runs of the two sides overlap. Use the web interface to
find out what is going on and the command when the answer has to survive being
quoted at somebody.

**One thing both get right that plain Locust would not:** Odoo reports its errors
inside a 200 response. A stock Locust test counts a server that answers every
request with "internal error" as a server answering every request. OdooBench
reads each response and marks those failed.

### Where to run the load generator

Locust runs one Python process, which uses one CPU core. Past a few hundred
requests a second that core is full, and from then on the generator sets the
pace rather than the server. Running it on the Odoo server itself makes this
worse, because the two compete for the same cores.

OdooBench records Locust's CPU warning for every run and prints it next to the
result, and `odoobench compare` refuses to endorse a comparison where either
side had it. On our own test server one smoke run dropped from 461 to 231
requests a second between two runs for exactly this reason. `--processes 4` spreads
the users over four Locust processes and lifts the one-core ceiling. It does not
stop the generator from competing with the server when both share a machine, so
for numbers you intend to quote, run it on a separate machine close to the
server.

## Getting enough data to measure on

A benchmark on an empty Odoo measures an empty Odoo. Every effect this tool can
show needs a table large enough that the database has to make a decision about
how to read it.

The best data is a restored copy of your production database. Failing that, Odoo
ships a generator:

```
odoo populate -d yourdb --models res.partner --factors 9
```

A factor of nine copies the model nine times, so it ends at ten times its
original size. Run it again to grow further. It needs a database user allowed to
create records, it holds a long transaction, and it is not quick: our 84.5
million partners came to 36 GB on disk.

**Generated rows are more regular than real ones**, and it changes what you
measure. In our set, records created close in time also sorted close together by
name. A date filter therefore found its matches in a dense stretch of the name
index instead of scattered through it, so the database walked far less of the
index than it would on real data. That distance is exactly what the `search`
scenario times.

Two consequences, both worth taking seriously:

- Compare configurations against each other **on the same data**. That is what
  this tool is for, and it stays valid.
- Do not compare your absolute numbers to ours, or to anybody else's. A second
  per page here and a second per page there are not the same second.

### Seeding data and seeding randomness

Two different things share the word. `odoo populate` seeds the *database*, once,
before you measure anything. The `--seed` flag seeds the *random generator* that
picks which page and which filter each simulated user asks for, on every run. It
defaults to a fixed value on purpose: both sides of a comparison then draw the
same parameters, and a difference between them cannot come from one side having
drawn easier work. Change it only when you want a different, equally repeatable
sequence.

## Scenarios

| Scenario | What it does | What a difference means |
| --- | --- | --- |
| `browse` | Opens lists and pages through them | How many requests Odoo's Python can serve at once. Moves with worker count and memory limits. Blind to database settings. |
| `search` | Filters a list by a date range, keeps the model's order | Whether the database walks the index or reads the table. Orders of magnitude on a large table. |
| `office-day` | 99 list views for every filtered search | Both bottlenecks, in the order they appear. The realistic one. |
| `month-end` | Groups a month of records | Whether an aggregation fits in the memory the database may use for one. |
| `analysis` | Opens a pivot and a graph on an analysis model | What a report costs: whether the aggregation fits in the memory the database allows one query, and how many cores it may use. |
| `document` | Prints a document twice, as HTML and as PDF | How long a printed document takes, and how much of that is the PDF engine rather than Odoo. |
| `document-batch` | Prints documents as PDF, in batches if asked | What a printing run costs, which is a different question from what one document costs. |
| `flat` | Sorts by a column with no index | Nothing, deliberately. The control: if this moves between two configurations, something other than the database changed. |

## Reports, which are two different things

"Report" means two unrelated things in Odoo, and they break in different places.
An **analysis** is a pivot or graph on a model like `sale.report`: one enormous
aggregation, no rendering. A **document** is a printed invoice or quotation: a
template, its queries, and an external program that turns HTML into a PDF. There
is a scenario for each.

### Analyses: pivots and graphs

```
odoobench analyses --url http://10.0.0.5:8069 --db production

odoobench run --url http://10.0.0.5:8069 --db production \
              --scenario analysis --analysis sale.report --months 12 --users 2
```

OdooBench reads the model's fields and picks a period, a dimension and a measure
by itself, then checks that the field it picked is actually filled in. A field
that is null everywhere makes a report that returns nothing and returns it very
fast, which is the most flattering wrong number a benchmark can produce. Override
any of it with `--group-period`, `--group-by` and `--measure`.

This is the query that can need more memory than the database allows one query,
and the one that uses spare cores. On 85 million records grouped by month and
country, our own settings against the defaults:

```
pivot              7782.5 ms ->     6339.9 ms
trend              3299.2 ms ->     2592.3 ms
```

Run it at a low concurrency. A server already saturated by list views has no
spare core left for an aggregation to use, so a busy machine hides the very
effect you are looking for.

### Documents: printed invoices and quotations

Find out what your instance can print, and whether it has anything to print:

```
odoobench documents --url http://10.0.0.5:8069 --db production
```

```
report                                    prints              records
account.report_invoice                    account.move              0
account_followup.report_followup_print_all res.partner       25116540
sale.report_saleorder                     sale.order                0
```

Then measure one:

```
odoobench run --url http://10.0.0.5:8069 --db production \
              --scenario document --document account_followup.report_followup_print_all \
              --users 2 --duration 60
```

The `document` scenario prints each one twice, once as HTML and once as PDF,
and files the two separately. On the instance above:

```
  html_x1             37.9 ms
  pdf_x1            2211.2 ms
```

Thirty-eight milliseconds of Odoo, and two and a fifth seconds of wkhtmltopdf.
Adding worker processes would not have moved that, and neither would touching the
database. Without the split you would have had a slow report and no idea which
half to go and fix.

Use `--document-batch 20` to print twenty records into one document, which is the
month-end print run rather than one invoice. Reports are also the request most
likely to run into a worker's time or memory limit, so errors in this scenario
are a finding rather than noise.

## The rules it follows

These are not style choices. Each one is a mistake we made first.

1. **Warm up, then throw the warm-up away.** The first requests of any run pay
   for caches every later request finds filled.
2. **Repeat the same configuration and keep the runs apart.** The report prints
   every run, not just their median.
3. **Overlapping runs mean no result.** If the runs of configuration A overlap
   the runs of configuration B, `odoobench compare` says so instead of reporting a
   percentage. We once had a 3% "win" that turned into a 2% loss on the next
   repeat.
4. **Failed requests are not fast requests.** Errors are counted separately and
   never enter the latency sample. A configuration that drops a third of the
   load is not the winner.
5. **The same seed on both sides.** Each worker seeds its own generator, so both
   sides of a comparison draw the same parameters.
6. **Compare latency at equal load.** `--rate` caps the arrival rate. Comparing
   per-request latency between a run that served 60 requests a second and one
   that served 500 compares a quiet machine with a busy one, and the busy one
   will look worse at exactly the things you improved.
7. **Break the numbers down by parameter.** A workload that mixes cheap and
   expensive filters hides the expensive ones in its median. The report lists
   each bucket separately.
8. **Say what the measurement cannot see.** Printed with every result.

## Traps this tool was built out of

- **Sorting by an unindexed column.** One possible plan, so database settings
  cannot matter and a benchmark built on it will say they never do.
- **Deep `OFFSET` as a stand-in for hard work.** It measures scan-and-discard,
  which is real but is not what your users do.
- **Measuring A, then B, on a cold cache.** The second one inherits a warm cache
  and wins for the wrong reason. Alternate.
- **Reading a per-request latency across runs with different throughput.** See
  rule 6.
- **Trusting a bucket with twenty samples.** Widen the run or narrow the
  workload; the report prints the count next to every bucket so you can see when
  a number rests on nothing.

## What it does not do

- It does not write. Nothing here inserts, updates or deletes, so vacuum,
  checkpoint and WAL settings are untouched by it.
- It does not render. This is the server's work, not the browser's.
- It does not tune anything. It measures; what you change is your decision.

## Licence

MIT. See [LICENSE](LICENSE).

Built at [hav.media](https://hav.media) while working out why our Odoo hosting
was slower than it needed to be.
