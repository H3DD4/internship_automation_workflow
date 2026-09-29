<?php
// ─────────────────────────────────────────────────────────────
//  ⚠️  VULNERABLE ON PURPOSE  ⚠️
//  This endpoint is the whole point of the workshop.
//  Bugs (deliberate):
//    1. No check of the file's real MIME / magic bytes.
//    2. No check of the file extension.
//    3. Original filename kept → attacker chooses ".php".
//    4. Files are saved inside the web root and are executable.
// ─────────────────────────────────────────────────────────────
session_start();
if (!isset($_SESSION['user'])) { header('Location: index.php'); exit; }
$user = $_SESSION['user'];

$uploads = __DIR__ . '/uploads';
if (!is_dir($uploads)) mkdir($uploads, 0777, true);

$msg = ''; $ok = false;

// Case 1: file picked from disk
if (!empty($_FILES['photo']['name'])) {
    $name = basename($_FILES['photo']['name']);      // <-- vulnerable: trust the user
    $dest = "$uploads/{$user}_$name";
    if (move_uploaded_file($_FILES['photo']['tmp_name'], $dest)) {
        $msg = "Saved as {$user}_$name"; $ok = true;
    } else { $msg = "Upload failed."; }
}
// Case 2: dataURL from the camera
elseif (!empty($_POST['captured'])) {
    $data = $_POST['captured'];
    if (preg_match('#^data:image/(\w+);base64,#', $data, $m)) {
        $bin = base64_decode(substr($data, strpos($data, ',') + 1));
        $fname = "{$user}_" . date('His') . "." . $m[1];
        file_put_contents("$uploads/$fname", $bin);
        $msg = "Saved as $fname"; $ok = true;
    } else { $msg = "Bad camera data."; }
} else {
    $msg = "No file provided.";
}
?>
<!doctype html><html><head>
<meta charset="utf-8"><title>Upload result</title>
<link rel="stylesheet" href="style.css"></head><body>
<div class="card">
  <h1>Upload</h1>
  <div class="<?= $ok ? 'ok' : 'err' ?>"><?= htmlspecialchars($msg) ?></div>
  <p><a href="dashboard.php">← Back</a></p>
</div>
</body></html>
