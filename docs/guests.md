# Let a few friends in with their Google account

Friends open a normal web address, sign in with Google, and get their own copy of the app: their own songs, guitars, My tone, quick picks and settings. They never see yours, and you never hand out Tailscale access. Your own Tailscale address keeps working exactly as before.

How it fits together:

- **Cloudflare Tunnel** (`cloudflared`) runs on the Mac mini and makes an outbound connection to Cloudflare. Nothing is opened on your router.
- **Cloudflare Access** sits in front of that address and only lets in the Google emails you list.
- **The app** listens for the tunnel on its own local port (8781). Every request on that port must carry Cloudflare's signed sign-in token, which the app checks against your Cloudflare team's keys before it believes the email. The Tailscale login header means nothing on that port, so nobody can pass as you.
- **The pedal** plugs into each person's own phone or laptop, in Chrome, with **Use pedal here**, just like you do on the go. A guest's pushes only ever reach the pedal that guest plugged in; the pedal on the Mac mini is never offered to them.
- **Claude** runs on your API key. Each guest gets 25 Claude jobs a day (generate, refine, fork or explain), and the Settings dialog shows them how many are left. Change it with `STOMPXL_GUEST_DAILY_CLAUDE`. You are never limited.

Free for a handful of people. The only cost is a domain name, about $10 a year.

## 1. A domain on Cloudflare (about 5 minutes)

1. Sign up at [dash.cloudflare.com](https://dash.cloudflare.com).
2. **Domain Registration > Register Domains**, and buy a cheap one (a `.com` is about $10 a year). Already own one elsewhere? **Add a domain** instead and follow its steps to point the nameservers at Cloudflare, on the Free plan.

The app will live at a subdomain such as `stomp.yourdomain.com`.

## 2. Zero Trust team (2 minutes)

1. In the dashboard, open **Zero Trust**.
2. Pick a team name, for example `my-band`. Your team domain becomes `my-band.cloudflareaccess.com`. Note it down.
3. Choose the **Free** plan (up to 50 people). It may ask for a card; Free isn't charged.

## 3. Google sign-in (10 minutes)

1. Go to [console.cloud.google.com](https://console.cloud.google.com), create a project called `StompXL sign-in`.
2. **APIs & Services > OAuth consent screen**: External, app name `StompXL`, your email as support and developer contact. Save. Under **Audience**, publish the app (sign-in only asks for name and email, so no review is needed).
3. **APIs & Services > Credentials > Create credentials > OAuth client ID**: type **Web application**.
   - Authorized JavaScript origins: `https://my-band.cloudflareaccess.com`
   - Authorized redirect URIs: `https://my-band.cloudflareaccess.com/cdn-cgi/access/callback`
   - Use your own team name in both. Copy the **Client ID** and **Client secret**.
4. Back in Zero Trust: **Settings > Authentication > Login methods > Add new > Google**. Paste the ID and secret, save, then **Test**: it should show your Google email.

## 4. The tunnel (admin account on the Mac mini, 5 minutes)

1. Zero Trust: **Networks > Tunnels > Create a tunnel > Cloudflared**, name it `stompxl`.
2. Choose **macOS**. It shows two commands. Run them in Terminal in your **admin** account (Homebrew lives there):

   ```bash
   brew install cloudflared
   sudo cloudflared service install <the long token it shows>
   ```

   The tunnel then starts with the Mac and shows **Healthy** in the dashboard.
3. Next, **Public hostname**: subdomain `stomp`, domain `yourdomain.com`, service type **HTTP**, URL `localhost:8781`. Save.

## 5. Who may sign in (5 minutes)

1. Zero Trust: **Access > Applications > Add an application > Self-hosted**.
2. Name `StompXL`, session duration **1 month** (so people aren't asked every day), domain `stomp.yourdomain.com`.
3. **Login methods**: tick only **Google**, and turn on **Instant Auth** so people go straight to Google.
4. **Policy**: name `Friends`, action **Allow**, Include **Emails**: your own Gmail plus each friend's Gmail.
5. Save. Open the application again and copy its **Application Audience (AUD) Tag** from the Overview tab.

## 6. Tell the app (the account the app runs in)

Open `~/.config/stompxl/stompxl.env` in the account the app runs as and add:

```bash
STOMPXL_CLOUDFLARE_PORT=8781
STOMPXL_CF_TEAM_DOMAIN=my-band.cloudflareaccess.com
STOMPXL_CF_AUD=<the AUD tag from step 5>
STOMPXL_GUEST_EMAILS=friend@gmail.com,bandmate@gmail.com
STOMPXL_OWNER_EMAILS=<your own Gmail>
```

`STOMPXL_OWNER_EMAILS` means signing in through Cloudflare with your own Google account opens **your** library, so you can use the app on a device without Tailscale too. Leave it out if you'd rather not.

Then restart from the admin account:

```bash
sudo launchctl kickstart -k system/com.stompxl.server
```

Check it: `https://stomp.yourdomain.com` should ask for Google, then open the app. The log line `Guest sign-in port (Cloudflare tunnel)` in `/Users/<app account>/Library/Logs/stompxl.log` confirms the app is listening for the tunnel.

## Adding or removing someone later

Two places, both needed: the **Friends** policy in Cloudflare (who can reach the page) and `STOMPXL_GUEST_EMAILS` (who has a library), then kickstart. Removing someone from either locks them out. Their songs stay on the Mac mini in `~/Library/Application Support/StompXL Tone Studio/users/<their email>/`, so adding them back brings everything back.

## What to tell a friend

1. Open `https://stomp.yourdomain.com` in **Chrome** (Android or a Mac/PC) and sign in with Google.
2. On Android: Chrome menu, **Add to Home screen**.
3. Add your guitars, then pick a rig and type a song.
4. To send presets: plug the HX Stomp XL into the phone (USB-C OTG cable) or laptop, tap **Use pedal here**, and keep the page open until a send finishes.

## Limits for guests

- **NAM captures from TONE3000** work for guests through **your** TONE3000 sign-in (sign in once on the Mac mini, as in mac-mini.md). They can't sign in or out, or change the key. Downloads land in their own folder, and **Download the IR files** in the NAM dialog gives them a zip to unzip on their computer and drag onto IR slots in HX Edit.
- **Open folder** for NAM captures is yours only (it opens Finder on the Mac mini).
- Their library lives on your Mac mini, so it's backed up when you back the Mac mini up.

## Troubleshooting

- **"Google sign-in isn't set up yet"**: `STOMPXL_CF_TEAM_DOMAIN` or `STOMPXL_CF_AUD` is missing from the env file.
- **"Sign in again"**: the token didn't check out. Usually the AUD tag is from a different application, or the team domain has a typo.
- **"isn't on its guest list"**: Cloudflare let them in but `STOMPXL_GUEST_EMAILS` doesn't list that email. Add it and kickstart.
- **Cloudflare error 502 / 1033**: the tunnel can't reach the app. Check the tunnel is Healthy and the env file has `STOMPXL_CLOUDFLARE_PORT=8781`.
