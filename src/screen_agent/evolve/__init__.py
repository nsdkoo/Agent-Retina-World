"""自进化：候选改进的生成、验证与档案库。

    from screen_agent.evolve import Archive, Evolver

    evolver = Evolver(evaluator, golden, Archive(path))
    for variant in evolver.step(failures):
        print(variant.summary())
"""

from screen_agent.evolve.loop import Archive, Evolver, Variant

__all__ = ["Archive", "Evolver", "Variant"]
