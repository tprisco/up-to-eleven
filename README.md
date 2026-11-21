# Up to Eleven?

A static site plus a Python script. You own both, and nothing needs Colgate IT.

    docs/index.html      the site (one file, rebuilt only if you change the design)
    docs/data/           everything the site shows: index.json, albums/*.json, covers/
    pipeline/measure.py  measures a ripped CD and writes the data files
    publish.sh           checks the data, commits and pushes

## One-time setup (about 15 minutes)

1. Make a free GitHub account if you do not have one, then create a new **public** repository named `up-to-eleven`.
2. In this folder run:

       git init -b main
       git add .
       git commit -m "First version"
       git remote add origin https://github.com/YOUR-NAME/up-to-eleven.git
       git push -u origin main

3. On GitHub: Settings, Pages, Source "Deploy from a branch", Branch `main`, folder `/docs`, Save.
   The site appears at `https://YOUR-NAME.github.io/up-to-eleven/` within a few minutes.
4. Add that origin (`https://YOUR-NAME.github.io`) to `ALLOWED_ORIGINS` in your Apps Script, then redeploy it, so the Contribute form works from the live site.
5. Install the script's packages once: `pip install -r pipeline/requirements.txt`
   (needs Python 3.9 or newer and nothing else; ffmpeg is not needed).

A custom address such as a colgate.edu subdomain is optional later; GitHub Pages supports it, and the site works without it.

## Every time a rip arrives

1. Download the files from your Drive "Review (checked)" folder into one folder, for example `~/Rips/rush-2112-1989`.
   Open the `.log` and check it says all tracks were accurately ripped.
2. Measure it:

       python3 pipeline/measure.py ~/Rips/rush-2112-1989 \
         --album-id rush-2112 --title "2112" --artist Rush --album-year 1976 \
         --year 1989 --label "Early CD" --catalog "822 545-2 M-1" --barcode "" \
         --cover ~/Rips/2112.jpg

   For a new version of an album already in the data, give only `--album-id --year --label --catalog`.
   Add `--dry-run` first to see the numbers without writing anything.
3. Publish: `./publish.sh "Add 2112, 1989 Early CD"`
4. Set the row's Status to "Published" in your Submissions sheet to send the contributor the thank-you email.

No change to the site is ever needed. The site reads `docs/data/` each time it loads.

## What gets measured

Integrated loudness (ITU-R BS.1770-4, gated as in EBU Tech 3341), loudness range (EBU Tech 3342), crest factor, sample peak, and the share of clipped samples, for each track and for the whole album. The loudness curve on the album page is short-term loudness (3 s windows) sampled at 100 points across the album, so versions line up in time. Measurements agree with ffmpeg's ebur128 filter to within 0.1 LU on the test files.

Rules: all tracks in a folder must share one sample rate, stereo or mono only. Files are ordered by the `.cue` if there is one, otherwise by file name.

`python3 pipeline/measure.py --check` validates every data file. `publish.sh` runs it first and stops on a problem.

## Notes

- Fixing a mistake: run the same command again with the same `--album-id`, `--year` and `--label`; it replaces that version. To remove one, delete it from `docs/data/albums/<id>.json` and fix the count in `index.json`, or just re-run for the other versions.
- Backups: the repository history is your backup of every data file.
- Keep the raw rips on your own drive; the repository holds only measurements and covers.
- Covers should be small JPGs (about 600 px) and are shown for identification.
