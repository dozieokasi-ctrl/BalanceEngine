# Privacy page update

Adds a public /privacy page, a footer link and support contact, and current Calendar/Sheets descriptions on the homepage. Contact address: dozieokasi@gmail.com (from your Google branding configuration). Review the policy before publishing, especially provider practices, operator access, and how you will handle deletion and information-copy requests. It documents current application behavior; creating this page does not complete Google verification or remove invitation-only registration.

Apply the ZIP to ~/BalanceEngine. Install existing requirements if needed and run:

```sh
BALANCEENGINE_ENV=test python -m unittest discover -s tests
git add public_beta.py templates/public/base.html templates/public/index.html templates/public/privacy.html tests/test_hosted_features.py PRIVACY_UPDATE.md
git --no-pager diff --cached --name-only
git diff --cached --check
git commit -m "Add public privacy policy and support links"
git push origin main
```

The suite has 21 tests. After Render reports Live, visit https://balanceengine-beta.onrender.com/privacy in a logged-out browser and confirm the page opens. Then use that actual URL in Google Auth Platform Branding's privacy-policy field. Existing account and integration settings are preserved. This package does not change your dashboard stylesheet.

Google domain ownership and brand/data-access verification remain separate. Do not enter a domain you cannot verify as your own. The Render subdomain's suitability must be established before submitting verification; a custom domain you control is an alternative.
