# Tender Hub authentication endpoint

Project: `prfluuygnxunaitzmttf`

`GET` or `POST https://prfluuygnxunaitzmttf.supabase.co/functions/v1/tender-hub`
requires a Tender Hub user access token in `Authorization: Bearer <access_token>`.
Pass the project's publishable key in `apikey`. Only the caller's user ID and
email are returned. No database writes, admin access or role assignment occur.

From the existing signed-in Supabase browser client:

```js
const { data, error } = await supabase.functions.invoke('tender-hub', { body: {} });
```

The SDK supplies the current session token. A publishable key alone is not a
user session. Browser requests are limited to the www and non-www production
Serene Creations origins. Preflight requests return 204.

The pinned `@supabase/server` package verifies user JWTs; the handler also checks
the user through Supabase Auth. Platform JWT verification remains enabled.
Supabase supplies the server environment automatically. No secret key needs to
be copied into the source or website, and the masked key is not used.

Deploy from the repository root with Supabase CLI:

```sh
supabase functions deploy tender-hub --project-ref prfluuygnxunaitzmttf
```

This function does not configure Google OAuth or migrate existing accounts.
