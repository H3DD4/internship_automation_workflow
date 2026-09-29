<?php
// ─────────────────────────────────────────────────────────────
//  INTENTIONALLY VULNERABLE — Securinets ENSIT teaching demo
//  Do not deploy publicly. Run only on localhost / lab network.
// ─────────────────────────────────────────────────────────────
session_start();

// Plaintext creds on purpose — this is one of the "loot" targets
// once the attacker gets a shell. In real code you'd hash these.
$USERS = [
    'alice' => 'sunshine123',
    'bob'   => 'password1',
    'yasmine' => 'ensit2025',
];

$err = '';
if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    $u = $_POST['username'] ?? '';
    $p = $_POST['password'] ?? '';
    if (isset($USERS[$u]) && $USERS[$u] === $p) {
        $_SESSION['user'] = $u;
        header('Location: dashboard.php');
        exit;
    }
    $err = 'Wrong username or password.';
}
?>
<!doctype html>
<html><head>
<meta charset="utf-8"><title>Securinets Photo Club — Login</title>
<link rel="stylesheet" href="style.css">
</head><body>
<div class="card">
  <h1>📸 Securinets Photo Club</h1>
  <p class="sub">Sign in to upload today's selfie.</p>
  <?php if ($err): ?><div class="err"><?= htmlspecialchars($err) ?></div><?php endif; ?>
  <form method="post">
    <label>Username</label>
    <input type="text" name="username" autofocus>
    <label>Password</label>
    <input type="password" name="password">
    <button type="submit">Log in</button>
  </form>
  <div class="hint">
    Demo accounts (for the workshop):<br>
    <code>alice / sunshine123</code> · <code>bob / password1</code> · <code>yasmine / ensit2025</code>
  </div>
</div>
</body></html>
