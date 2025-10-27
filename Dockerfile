FROM nextcloud:latest

ARG VERSION=31.0.10

# Update package lists
RUN apt-get update

# Install ffmpeg
RUN apt-get install -y ffmpeg

# Install nano
RUN apt-get install -y nano

#Install Node & NPM
RUN apt-get install -y nodejs npm
