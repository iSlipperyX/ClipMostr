"""The editor: turns a timeline (studio/timeline_schema.py) into a video in one ffmpeg run.

    kinetic_subtitles.py  timed words to a caption track and its .ass file
    motion_engine.py      keyframes: values, ffmpeg expressions, ready-made moves
    audio_mixer.py        voice, music and effects buses, ducking, loudness
    timeline_compiler.py  the filter graph and the render
"""
