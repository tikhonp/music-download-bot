# music-download-bot

A Telegram bot that downloads music from Qobuz to specified directory

## Running with Docker

1. Create a `.env` file in the project root like `.env.example`

2. Build the Docker image:

   ```bash
   docker build -t music-download-bot .
   ```

3. Run the Docker container with the necessary environment variables:

   ```bash
   docker run --env-file .env -v "/Users/tikhon/Desktop/untitled folder:/downloads" -v "$HOME/.config/qobuz-dl:/qobuz-dl" md-bot
