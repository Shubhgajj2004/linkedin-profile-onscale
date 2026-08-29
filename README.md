# LinkedIn Profile API
I won't unnecessarily complicate stuff with system design, and caching and blabla. What matters most that only I will cover in this, but it will have potential to handle request on scale. which gonna be huge :)

## Goal
Turns a LinkedIn profile URL into structured JSON through LinkedIn's authenticated Voyager API.

## What matters right now?
- Prevent account from banning on scale.
- ensuring either of jsessionid or li_at does not become stale. Otherwise calling linkedin voyager API with stale creds, create account restriction checkpoints for linkedin teams.

## Future Scope
- *Rate limiting* requests, based on IPs. So one person don't abuse.
- *Caching* the results, to server without actual calling linkedin's Voyager API.

## How to prevent account from banning?
- Acquire proxy server services. Want help :)?
- - use this: https://webshare.io. It gives 10 proxies for FREE.
- Residential proxy servers are good, and static residential proxies are best ঌ

-------
# Setup
```
cp .env.example .env
```
### *.env* looks like this
```env
HEALTH_API_KEY=replace-me
LINKEDIN_ACCOUNTS_JSON='[{"account":"account-one","proxy_address":"proxy-one.example.com","proxy_port":6754,"proxy_username":"user-one","proxy_password":"pass-one","location":"London, UK","li_at":"li-at-one","jsessionid":"ajax:one","user_agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"},{"account":"account-two","proxy_address":"proxy-two.example.com","proxy_port":6754,"proxy_username":"user-two","proxy_password":"pass-two","location":"New York, US","li_at":"li-at-two","jsessionid":"ajax:two","user_agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"}]'
```
---
# Run
```b
python -m venv .venv
source .venv/bin/activate //for mac
//or
.\.venv\Scripts\Activate.ps1 //for windows
pip install -r requirements.txt
uvicorn app.main:app --reload
```
---
# Usage
```bash
curl http://localhost:8000/v1/profile \
  -H 'content-type: application/json' \
  -d '{"url":"https://www.linkedin.com/in/vinod-khosla-65387416/"}'
```

Returns metadata, identity, headline, location, about, industry, images, experience, education, skills, certifications, and languages.

### Check Accounts

```bash
curl http://localhost:8000/health \
  -H 'X-API-Key: replace-me'
```

Calls LinkedIn's `/me` once for each healthy account. Failed accounts stay out of rotation until their cookies are replaced and the app is restarted.

---
## Approach

```text
Client → API → Account + Proxy Pool → LinkedIn Voyager → JSON
```

1. Validate the LinkedIn profile URL.
2. Pick the next available account and its proxy.
3. Call LinkedIn Voyager through that proxy.
4. Try `dash/profiles` first. Fall back to `profileView` if needed.
5. Convert LinkedIn's response into clean JSON.
6. Put the account-proxy pair back in the pool.

- Each LinkedIn account always uses its assigned proxy.
- Requests rotate between available pairs, similar to round robin.
- One account handles only one request at a time.
- Accounts returning a login redirect, `401`, or `403` leave the healthy pool.
- This spreads requests across accounts and IPs, reducing ban risk.
- Profiles are not stored.


## Limitations

- Voyager is private and can change.
- Visibility depends on the session, profile privacy, region, and relationship.
- If some legal issue happens with Linkedin Team, then don't call me, because it's again their terms. :)
