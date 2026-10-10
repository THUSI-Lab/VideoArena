# Attribution and licence

## What these videos are

Every video here is **synthetic**: a 3D render produced by this pipeline. No
match footage is redistributed, in whole or in part, and no frame of any
video here originated from a camera. The court, the two figures, the rackets and
the shuttle are drawn from geometry.

This matters for more than provenance. Because the renderer is ours, the absence
of a score display is *structural* rather than measured: the render path never
reads a score and never loads a font, so there is nothing for a pixel test to
find. Each `*_gt.json` records this as `hud_audit.kind = "structural"`.

## What is derived from someone else's work

The trajectories are derived from **ShuttleSet** (KDD 2023) stroke-level
annotations -- rally timings, shot landing coordinates, player positions and the
running score. The renders and the ground truth in this release are derivative
works of those annotations.

ShuttleSet is distributed as part of the CoachAI-Projects repository under the
MIT licence, reproduced in full below as that licence requires. It permits use,
modification and redistribution, including of derivative works.

Please also cite the dataset, which its authors request:

    @article{ShuttleSet,
      author  = {Wei-Yao Wang and Yung-Chang Huang and Tsi-Ui Ik and Wen-Chih Peng},
      title   = {ShuttleSet: A Human-Annotated Stroke-Level Singles Dataset for
                 Badminton Tactical Analysis},
      journal = {CoRR},
      volume  = {abs/2306.04948},
      year    = {2023}
    }

## What is deliberately absent

No player name, country, tournament, match identifier, source URL or source
directory name appears anywhere in this release. That is enforced by a scan over
the released files, not by convention. The dataset name above is a citation --
it identifies the corpus, not which match any case came from.

## Upstream licence, in full

MIT License

Copyright (c) 2022 Wei-Yao Wang

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
