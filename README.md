# Hoop Numbers

NBA picks from simulated games. `index.html` is the whole site. Every night a GitHub robot
(`update_ratings.py`) downloads the new games from ESPN, rebuilds every player's on/off rating,
and saves `ratings_<season>.json`. Netlify republishes the site automatically after each run.

## One-time setup (about 10 minutes)

1. **GitHub:** sign in at github.com, click **New repository**, name it `hoop-numbers`, keep it
   **Public** (free unlimited robot minutes), and create it.
2. **Upload the files:** on the new repo page click **uploading an existing file**, then drag in
   *everything* from this folder, including the `.github` folder and the `data` folder. Click
   **Commit changes**.
   - If the `.github` folder doesn't upload (some browsers hide it): click **Add file → Create new file**,
     type the name `.github/workflows/nightly.yml`, paste the contents of that file, and commit.
3. **Let the robot save files:** repo **Settings → Actions → General → Workflow permissions →
   Read and write permissions → Save**.
4. **Run it the first time:** **Actions** tab → **Nightly ratings** → **Run workflow**. The first run takes
   about 20-30 minutes (it reads last season too, as a starting point). After that it runs every
   morning by itself and takes a few minutes.
5. **Netlify:** open your site (nbasimpicks) → **Site configuration → Build & deploy → Continuous
   deployment → Link repository** → GitHub → pick `hoop-numbers`. Leave the build command empty and
   set the publish directory to `.` (a single dot). Save. From now on every robot run republishes the site.

## Check it's working

- The page footer says **"Player on/off ratings updated <date> (<games> games)"**.
- On GitHub, the **Actions** tab shows a green check each morning.
- If the footer says the ratings file wasn't found, run step 4 again and look at the red error in the Actions log.

## Opening the page from your computer

`index.html` also works when opened straight from your Downloads folder: it reads the ratings from
`https://nbasimpicks.netlify.app/`. If your Netlify address is different, change `RATINGS_BASE` near
the top of the script in `index.html`.
