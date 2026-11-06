"""Aziz manages one GPU machine: who gets the GPU, when, and in what order.

The local model (Kamil) stays loaded and ready whenever it can. The image app always wins. Heavy jobs say whether they
need the GPU, and Aziz decides when they run: CPU jobs leave the model alone, and a GPU job sends the model to
disk, runs, and the model loads straight back, unless the sender said more GPU work is likely soon.

The order: the image app, then a student who asked for priority, then GPU jobs, then the idle model.

Two modes. AI-Asistan (`assistant`) is all of the above. Compute (`compute`) leaves the GPU to work: the same order, with
the model kept off until the mode changes back.
"""
