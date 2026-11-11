"""The Studio: a prompt or an article in, a finished video and its editable timeline out.

    scraper.py          reads a public article (title, publisher, text, address)
    scriptwriter.py     writes the script and storyboard (short, or long by chapter)
    voice_studio.py     the narration: local voices, or a cloud voice with your key
    visual_sourcer.py   each scene's picture: your B-roll, stock sites with your key, or made on this PC
    timeline_schema.py  the timeline: one JSON document that says everything about the video
    orchestrator.py     runs it all and hands the video to posting

The editing itself (captions, motion, sound, the one ffmpeg run) is ytclip/editor.
"""
