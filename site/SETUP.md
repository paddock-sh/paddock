# Wiring up paddock.desquaredp.com

Everything the site needs is in this directory: `index.html`, `logo.webp` (the
page uses it) and `logo.png` (the social card). No build step, no dependencies.
Point a static host at `site/` and it works.

One thing is not a file: `/install.sh`. That path has to be a **redirect or a
proxy** to the script in the repository, never a copy. A copy is a second script
to remember to update, and the day it drifts from the real one is the day it
installs the wrong thing.

## 1. DNS

Point `paddock` at the host, then add the domain in the host's dashboard so it
issues a certificate.

**Vercel, Netlify, Cloudflare Pages and the like.** They give you a hostname to
alias:

```
paddock    CNAME    cname.vercel-dns.com.
paddock    CNAME    <site-name>.netlify.app.
paddock    CNAME    <project>.pages.dev.
```

**A plain static host or your own box.** If it is an apex you cannot CNAME, use
an address record instead:

```
paddock    A        203.0.113.10
paddock    AAAA     2001:db8::10
```

TTL 300 while you are setting it up, then raise it once it resolves.

## 2. The `/install.sh` route

The target is always:

```
https://raw.githubusercontent.com/desquaredp/paddock/main/install.sh
```

The page's one-liner is `curl -fsSL`, and the `L` in that follows redirects, so
a 301 or 302 is enough. A proxy rewrite works too and is slightly kinder,
because it also serves callers who left `-L` off.

**Netlify.** A `_redirects` file next to `index.html`:

```
/install.sh  https://raw.githubusercontent.com/desquaredp/paddock/main/install.sh  302
```

Change the `302` to `200!` if you would rather proxy than redirect.

**Vercel.** In `vercel.json` at the repository root:

```json
{
  "rewrites": [
    {
      "source": "/install.sh",
      "destination": "https://raw.githubusercontent.com/desquaredp/paddock/main/install.sh"
    }
  ]
}
```

A `rewrites` entry proxies. Use `redirects` with `"permanent": false` if you
want the browser to see the GitHub URL.

**Cloudflare Pages.** Same `_redirects` file as Netlify.

**nginx.** Redirect:

```nginx
location = /install.sh {
    return 302 https://raw.githubusercontent.com/desquaredp/paddock/main/install.sh;
}
```

Or proxy, which keeps the domain in the address the user sees:

```nginx
location = /install.sh {
    proxy_pass https://raw.githubusercontent.com/desquaredp/paddock/main/install.sh;
    proxy_set_header Host raw.githubusercontent.com;
    proxy_ssl_server_name on;
}
```

**Caddy.**

```
paddock.desquaredp.com {
    root * /srv/paddock/site
    redir /install.sh https://raw.githubusercontent.com/desquaredp/paddock/main/install.sh 302
    file_server
}
```

## 3. The repository has to be public first

`raw.githubusercontent.com` returns 404 for a private repository, whatever the
redirect says. Until `desquaredp/paddock` is public, the short one-liner cannot
work for anyone but you.

The page can go up before then. Swap the command in the hero for the one that
works today, which is the same command the README carries:

```html
<!-- while the repo is private -->
<code id="cmd"><span class="s">$ </span>uv tool install git+https://github.com/desquaredp/paddock</code>
```

```html
<!-- once it is public, and the redirect is live -->
<code id="cmd"><span class="s">$ </span>curl -fsSL https://paddock.desquaredp.com/install.sh | sh</code>
```

The copy button reads whatever is in that element, so nothing else changes.

## 4. Once it is live

Check the route before you tell anyone about it:

```sh
curl -sSI https://paddock.desquaredp.com/install.sh | head -3   # 302 or 200
curl -fsSL https://paddock.desquaredp.com/install.sh | head -3  # the script itself
```

Then the short form can replace the long one in two places:

- `README.md`, under **Install**
- `docs/RELEASING.md`, wherever the install line appears

Both currently name `raw.githubusercontent.com` directly. That form keeps
working, so this is a tidiness change and not a required one.

## Notes

- The page is one self-contained file. It opens over `file://` with the two
  images beside it, so you can check a change without a server.
- The only outbound request it makes is one stylesheet from Google Fonts. There
  are no trackers, no analytics and no cookies. Deleting the two `<link
  rel="preconnect">` lines and the stylesheet link leaves the page working on
  its fallback serif, if you would rather it made no outbound request at all.
- `logo.png` exists for `og:image` only. Social crawlers are unreliable about
  WebP, so the card gets a PNG and the page gets the WebP.
