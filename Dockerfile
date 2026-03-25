# FROM nextcloud:32.0.6

# ARG VERSION=32.0.6

# # Update package lists
# RUN apt-get update

# # Install ffmpeg
# RUN apt-get install -y ffmpeg

# # Install nano
# RUN apt-get install -y nano

# #Install Node & NPM
# RUN apt-get install -y nodejs npm

# # list directories
# RUN ls -lh


FROM nextcloud:32.0.6
ARG VERSION=32.0.6
 
# Update package lists
RUN apt-get update
 
# Install ffmpeg
RUN apt-get install -y ffmpeg
 
# Install nano
RUN apt-get install -y nano
 
# Install Node & NPM
RUN apt-get install -y nodejs npm
 
# Install Java (needed by libresign)
RUN apt-get install -y default-jre-headless
 
# Copy the occ hook script — runs on first container start after Nextcloud is ready
COPY init-libresign.sh /docker-entrypoint-hooks.d/post-installation/init-libresign.sh
RUN chmod +x /docker-entrypoint-hooks.d/post-installation/init-libresign.sh
 
# List directories (optional debug step)
RUN ls -lh
