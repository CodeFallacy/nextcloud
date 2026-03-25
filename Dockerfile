FROM nextcloud:32.0.6

ARG VERSION=32.0.6

# Update package lists
RUN apt-get update

# Install ffmpeg
RUN apt-get install -y ffmpeg

# Install nano
RUN apt-get install -y nano

#Install Node & NPM
RUN apt-get install -y nodejs npm

# list directories
RUN ls -lh
