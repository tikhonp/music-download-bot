# music-download-bot

A Telegram bot that downloads music from Qobuz to specified directory

## Running with Docker

1. Create a `.env` file in the project root like `.env.example`

2. Run the Docker container with the necessary environment variables:

   ```sh
   docker run --env-file .env \
     -v "/path/to/your/downloads:/downloads" \
     -v "$HOME/.config/qobuz-dl:/qobuz-dl" \
     ghcr.io/tikhonp/music-download-bot:latest
   ```

## Compose file

```yaml
services:
  music-download-bot:
    image: ghcr.io/tikhonp/music-download-bot:latest
    env_file:
      - .env
    volumes:
      - "/path/to/your/downloads:/downloads"
      - "$HOME/.config/qobuz-dl:/qobuz-dl"
```

## Development

```sh
# Create env
python -m venv env
. env/bin/activate
pip install -r requirements.txt
# point pylsp to this env
export PYTHONPATH=env/lib/python3.14/site-packages
nvim
```
