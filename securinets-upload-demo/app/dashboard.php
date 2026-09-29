<?php
session_start();
if (!isset($_SESSION['user'])) { header('Location: index.php'); exit; }
$user = $_SESSION['user'];

// List this user's already-uploaded photos
$mine = [];
foreach (glob(__DIR__ . '/uploads/*') as $f) {
    if (strpos(basename($f), $user . '_') === 0) $mine[] = basename($f);
}
?>
<!doctype html>
<html><head>
<meta charset="utf-8"><title>Dashboard — <?= htmlspecialchars($user) ?></title>
<link rel="stylesheet" href="style.css">
</head><body>
<div class="card">
  <h1>Hi, <?= htmlspecialchars($user) ?> 👋</h1>
  <p class="sub">Take a selfie with your camera, or upload one from your device.</p>

  <video id="cam" autoplay playsinline></video>
  <canvas id="canvas" style="display:none"></canvas>

  <div class="row">
    <button id="snap" type="button">📷 Snap</button>
    <button id="retake" type="button" style="background:#4a4e69">↺ Retake</button>
  </div>

  <form id="uploadForm" method="post" action="upload.php" enctype="multipart/form-data" style="margin-top:20px">
    <label>Or pick a file:</label>
    <input type="file" name="photo" id="fileInput" accept="image/*">
    <input type="hidden" name="captured" id="captured">
    <button type="submit">⬆️ Upload</button>
  </form>

  <?php if ($mine): ?>
    <h3 style="margin-top:24px">Your photos</h3>
    <?php foreach ($mine as $p): ?>
      <img class="preview" src="uploads/<?= htmlspecialchars($p) ?>">
    <?php endforeach; ?>
  <?php endif; ?>

  <p style="margin-top:20px"><a href="logout.php">Log out</a></p>
</div>

<script>
const video = document.getElementById('cam');
const canvas = document.getElementById('canvas');
const snap = document.getElementById('snap');
const retake = document.getElementById('retake');
const captured = document.getElementById('captured');

navigator.mediaDevices.getUserMedia({ video: true }).then(s => video.srcObject = s)
  .catch(e => { document.getElementById('cam').style.display='none'; });

snap.onclick = () => {
  canvas.width = video.videoWidth; canvas.height = video.videoHeight;
  canvas.getContext('2d').drawImage(video, 0, 0);
  canvas.style.display = 'block'; video.style.display = 'none';
  captured.value = canvas.toDataURL('image/png');
};
retake.onclick = () => {
  canvas.style.display = 'none'; video.style.display = 'block';
  captured.value = '';
};
</script>
</body></html>
