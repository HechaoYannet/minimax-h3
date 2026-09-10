#!/usr/bin/env python3
"""h3_fetch_processor.py -- one-off fetch of the Ref2VA processor + tokenizer.

The pipeline needs MiniMax/MiniMax-H3 Ref2VA/processor/ (11 MiB: tokenizer.json,
vocab.json, merges.txt, tokenizer_config.json, chat_template.json,
preprocessor_config.json, video_preprocessor_config.json).  Without it
MiniMaxH3Pipeline.from_pretrained(processor_config=...) cannot build a prompt.

Nothing else from that repo is downloaded -- the NF4 weights already on disk are
used for everything else.  HuggingFace is unreachable from this network; the
ModelScope mirror is what the framework defaults to.
"""
import os, sys

LOCAL = os.environ.get("H3_MODELS", "/home/yhc/source/minimax-h3/models")
DEST = os.path.join(LOCAL, "MiniMax-H3")


def main():
    if os.path.isdir(os.path.join(DEST, "Ref2VA", "processor")):
        print("already present: " + DEST + "/Ref2VA/processor")
        return 0
    from modelscope import snapshot_download
    path = snapshot_download("MiniMax/MiniMax-H3", local_dir=DEST,
                             allow_file_pattern=["Ref2VA/processor/*", "Ref2VA/tokenizer/*"])
    print("downloaded to:", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
