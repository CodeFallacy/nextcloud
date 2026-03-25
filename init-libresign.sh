#!/bin/bash
set -e
 
echo "Running libresign Java install..."
php /var/www/html/occ libresign:install --java
echo "libresign Java install complete."
 
