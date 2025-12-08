FROM nextcloud:latest

ARG VERSION=32

# Update package lists
RUN apt-get update

# Install ffmpeg
RUN apt-get install -y ffmpeg

# Install nano
RUN apt-get install -y nano

#Install Node & NPM
RUN apt-get install -y nodejs npm

# Install OpenJDK
RUN apt-get install openjdk
