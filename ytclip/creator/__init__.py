"""The Video Creator: original faceless videos made from a prompt, entirely on this PC.

A prompt ("3 stoic habits that will change your life") or your own script goes in; a finished
1080x1920 video with a voiceover, moving pictures, music, sound effects and kinetic captions comes out,
in the category's Automation folder, from where it's queued for the category's TikTok account like any
clip. Every step runs locally and costs nothing per video:

* director.py writes the script and the storyboard with the local AI (llama.cpp, Settings → AI), its
  answer held to a JSON grammar so it always parses; or splits your own script into scenes;
* voice_engine.py reads it out with a neural voice on this PC (Kokoro-82M, Piper; Windows' own voices
  or eSpeak when neither is installed) and times every word (whisper, else from the sound itself);
* visual_generator.py makes each scene's picture: Stable Diffusion (SDXL-Turbo / FLUX.1-schnell
  through diffusers, or a ComfyUI running on this PC), your own B-roll, or built-in moving textures;
* motion.py moves the camera over still pictures (Ken Burns push-ins, pull-outs and pans);
* sfx_library.py and soundbed.py supply the sound effects and, without a music folder, a music bed,
  all made on this PC (or your own files), so nothing needs a licence;
* compositor.py builds the ffmpeg graphs: each scene, the transitions, the captions, the sound mix
  (voice at -14 LUFS, music ducked under it, the effects, a -1 dBTP ceiling);
* orchestrator.py runs it all, checks the result and hands it to the posting queue.

Every video it makes is labelled AI-generated when it's posted (TikTok requires that label on
realistic AI-made content) and never promises a part 2 that doesn't exist.
"""
