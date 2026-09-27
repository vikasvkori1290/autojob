# AutoJobApply Deployment Guide

This guide explains how to deploy the **AutoJobApply** AI job search, scraping, and fit ranking platform across different environments:
1. [Vercel Serverless Deployment](#1-vercel-deployment) — *Fastest 1-click cloud deployment*
2. [Free Cloud Hosting (Render)](#2-free-cloud-hosting-render) — *Best for long background scrapes*
3. [Railway Cloud Deployment](#3-railway-cloud-deployment)
4. [Docker Container (Self-Hosted / VPS / AWS EC2)](#4-docker-container-deployment)
5. [Instant Mobile Access (Cloudflare Tunnel)](#5-instant-mobile--remote-access-cloudflare-tunnel)

---

## 1. Vercel Deployment

The project includes pre-configured [vercel.json](file:///c:/devoloper/AutoJobApply/vercel.json) and [api/index.py](file:///c:/devoloper/AutoJobApply/api/index.py) serverless entry points.

### Step 1: Push Code to GitHub
```bash
git add .
git commit -m "Add Vercel deployment configuration"
git push origin main
```

### Step 2: Import into Vercel
1. Log in to **[Vercel.com](https://vercel.com/)** using your GitHub account.
2. Click **"Add New..."** &rarr; **"Project"**.
3. Under **Import Git Repository**, find your `AutoJobApply` repo and click **Import**.

### Step 3: Configure Project Settings
- **Framework Preset:** Leave as `Other`.
- **Root Directory:** `./` (default).
- **Build and Output Settings:** Leave defaults (Vercel automatically detects `requirements.txt` and `api/index.py`).

### Step 4: Add Environment Variables
Under the **Environment Variables** collapsible section, add:
| Key | Value |
| :--- | :--- |
| `NVIDIA_API_KEY` | *(your NVIDIA NIM API key)* |
| `HEADLESS` | `1` |

*(Optional: If using Google Gemini, add `GEMINI_API_KEY` as well)*.

### Step 5: Deploy
Click **Deploy**.
Vercel will install the Python dependencies and launch your application at a URL like:
`https://autojobapply.vercel.app`

> **Note on Vercel Serverless Limits:**
> Vercel's free tier has a 10-15 second timeout per serverless request. Live searches on Internshala or quick LinkedIn checks will run smoothly. For very deep scrapes with 100+ jobs, Render or Docker (Options 2 & 4) is recommended as they run continuous long-running background tasks.

---

## Environment Variables Reference

Before deploying, ensure you have your environment variables ready:

| Variable | Description | Default | Required? |
| :--- | :--- | :--- | :---: |
| `NVIDIA_API_KEY` | Free API key from [build.nvidia.com](https://build.nvidia.com) | *(none)* | **Yes** (or `GEMINI_API_KEY`) |
| `GEMINI_API_KEY` | Free API key from [aistudio.google.com](https://aistudio.google.com) | *(none)* | Optional alternative |
| `HOST` | Bind host address (`0.0.0.0` for containers/cloud) | `127.0.0.1` | Set to `0.0.0.0` on cloud |
| `PORT` | Listening port for web server | `4000` | Auto-set by cloud provider |
| `HEADLESS` | Set to `1` to prevent server attempting to open a browser window | `0` | Set to `1` on cloud/docker |

---

## 2. Free Cloud Hosting (Render)

[Render.com](https://render.com) offers a free tier suitable for running Python web services.

### Step 1: Push Code to GitHub
Ensure all your files are committed and pushed to your GitHub repository:
```bash
git add .
git commit -m "Add deployment files"
git push origin main
```

### Step 2: Create a Web Service on Render
1. Go to **[dashboard.render.com](https://dashboard.render.com/)** and sign in using your GitHub account.
2. Click **New +** &rarr; **Web Service**.
3. Select your `AutoJobApply` repository.
4. Fill in the service configuration:
   - **Name:** `autojobapply` (or any name you choose)
   - **Region:** Choose the region nearest to your target job market (e.g., Singapore or Frankfurt)
   - **Branch:** `main`
   - **Runtime:** `Python 3`
   - **Build Command:**
     ```bash
     pip install -r requirements.txt
     ```
   - **Start Command:**
     ```bash
     python -m job_scraper.gui --port $PORT --no-browser
     ```
   - **Plan:** Free

### Step 3: Add Environment Variables
Scroll down to the **Environment Variables** section and click **Add Environment Variable**:
- Key: `NVIDIA_API_KEY` &rarr; Value: `your_actual_nvidia_key`
- Key: `HOST` &rarr; Value: `0.0.0.0`
- Key: `HEADLESS` &rarr; Value: `1`

### Step 4: Deploy
Click **Create Web Service**. Render will automatically build the environment and provide you with a live HTTPS address (e.g., `https://autojobapply.onrender.com`).

---

## 3. Railway Cloud Deployment

[Railway.app](https://railway.app/) can automatically detect the included `Dockerfile`.

1. Go to **[railway.app](https://railway.app/)** and sign in with GitHub.
2. Click **New Project** &rarr; **Deploy from GitHub repo**.
3. Select your `AutoJobApply` repository.
4. Go to the project **Variables** tab and add:
   - `NVIDIA_API_KEY` = `your_nvidia_api_key`
   - `HOST` = `0.0.0.0`
   - `PORT` = `4000`
   - `HEADLESS` = `1`
5. Under **Settings** &rarr; **Networking**, click **Generate Domain**.
6. Railway will build the container using `Dockerfile` and serve your app.

---

## 4. Docker Container Deployment

If you are running on your own VPS (Ubuntu/Debian), DigitalOcean droplet, or AWS EC2 instance:

### Option A: Using Docker Compose (Recommended)
1. Clone your repo onto the server:
   ```bash
   git clone <your-repo-url>
   cd AutoJobApply
   ```
2. Copy your `.env` file with your `NVIDIA_API_KEY`:
   ```bash
   cp .env.example .env
   # Edit .env with your favorite editor (nano .env)
   ```
3. Start the container in the background:
   ```bash
   docker compose up -d --build
   ```
4. Check running status:
   ```bash
   docker compose logs -f
   ```
   Your app will be live at `http://<your-server-ip>:4000`.

### Option B: Using Plain Docker CLI
```bash
# 1. Build the image
docker build -t autojobapply .

# 2. Run the container
docker run -d \
  --name autojobapply \
  -p 4000:4000 \
  --env-file .env \
  --restart unless-stopped \
  autojobapply
```

---

## 5. Instant Mobile / Remote Access (Cloudflare Tunnel)

If you already run the application locally on your laptop (`npm run gui`) and simply want to open the dashboard on your phone or share it with a friend **without deploying to the cloud**:

1. Keep your local server running (`npm run gui`).
2. Open a separate terminal and run:
   ```bash
   npx cloudflared tunnel --url http://localhost:4000
   ```
3. Look for the output line:
   ```
   Your quick Tunnel has been created! Visit it at (it may take some time to be reachable):
   https://random-subdomain.trycloudflare.com
   ```
4. Open that HTTPS URL on your phone browser — you can search, filter, and score jobs on your mobile device while your computer handles the processing!

---

## Verifying Deployment Health

Once deployed, you can verify your service status using the built-in REST endpoint:
```bash
curl https://<your-domain>/api/settings
```
Expected response:
```json
{
  "api_key_configured": true,
  "rubric": { ... }
}
```
