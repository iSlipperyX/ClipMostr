"""ClipMonster's Telegram bot and Mini App: ask for videos, watch what's waiting to post, critique and approve
them, and delete bad ones, from a phone.

  bot.py              the dispatcher automation runs: who may speak, commands, buttons, the polling threads
  studio_handler.py   videos, clips and requests asked for in words; which category an idea is for
  sports_handler.py   /games, /reel, /sports, and /find: a moment searched for in a game
  review_pipeline.py  the video cards, Critique / Regenerate, v1 and v2, approving one
  streamer.py         the video as Telegram takes it (a mobile preview of a big master), and the one progress
                      message that's edited in place
  mini_app.py         the Mini App's server; mini_app_page.py its page
  state.py            telegram.json: the linked people, the pairing code, critiques and versions
  api.py              the Bot API over HTTPS: rate limits kept, 429s waited out, uploads sent in pieces
  pair.py             linking a phone from the window;  tunnel.py  the Mini App's public address

(preview.py, webapp.py and webapp_page.py are the old names of streamer.py, mini_app.py and mini_app_page.py.)
"""
