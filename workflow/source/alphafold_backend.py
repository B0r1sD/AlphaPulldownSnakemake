import time
import json
import pickle

import numpy as np
from os.path import join, exists
from typing import List, Dict

import run_alphafold
import jax.numpy as jnp
from run_alphafold import ModelsToRelax
from alphafold.relax import relax
from alphafold.common import protein, residue_constants
from alphapulldown.predict_structure import get_existing_model_info
from alphapulldown.objects import MultimericObject
from alphapulldown.utils import (
    create_and_save_pae_plots,
    post_prediction_process,
)

from folding_backend import FoldingBackend


def _jnp_to_np(output):
    """Recursively changes jax arrays to numpy arrays."""
    for k, v in output.items():
        if isinstance(v, dict):
            output[k] = _jnp_to_np(v)
        elif isinstance(v, jnp.ndarray):
            output[k] = np.array(v)
    return output


class AlphaFold(FoldingBackend):

    @staticmethod
    def predict(
        model_runners,
        output_dir,
        feature_dict,
        random_seed,
        fasta_name: str,
        models_to_relax: object = ModelsToRelax,
        allow_resume=True,
        seqs: List = [],
        use_gpu_relax: bool = True,
        multimeric_mode: bool = False,
        **kwargs
    ):
        timings = {}
        unrelaxed_pdbs = {}
        relaxed_pdbs = {}
        relax_metrics = {}
        ranking_confidences = {}
        unrelaxed_proteins = {}
        prediction_result = {}
        START = 0

        ranking_output_path = join(output_dir, "ranking_debug.json")

        if allow_resume:
            (
                ranking_confidences,
                unrelaxed_proteins,
                unrelaxed_pdbs,
                START,
            ) = get_existing_model_info(output_dir, model_runners)

            if exists(ranking_output_path) and len(unrelaxed_pdbs) == len(
                model_runners
            ):
                START = len(model_runners)

        num_models = len(model_runners)
        for model_index, (model_name, model_runner) in enumerate(model_runners.items()):
            if model_index < START:
                continue
            t_0 = time.time()

            model_random_seed = model_index + random_seed * num_models
            processed_feature_dict = model_runner.process_features(
                feature_dict, random_seed=model_random_seed
            )
            timings[f"process_features_{model_name}"] = time.time() - t_0
            # Die if --multimeric_mode=True but no non-zero templates are in the feature dict
            if multimeric_mode:
                if "template_all_atom_positions" in processed_feature_dict:
                    if not np.any(
                        processed_feature_dict["template_all_atom_positions"]
                    ):
                        raise ValueError(
                            "No valid templates found: all positions are zero."
                        )
                else:
                    raise ValueError(
                        "No template_all_atom_positions key found in processed_feature_dict."
                    )

            t_0 = time.time()
            prediction_result = model_runner.predict(
                processed_feature_dict, random_seed=model_random_seed
            )

            # update prediction_result with input seqs
            prediction_result.update({"seqs": seqs})

            t_diff = time.time() - t_0
            timings[f"predict_and_compile_{model_name}"] = t_diff

            plddt = prediction_result["plddt"]
            ranking_confidences[model_name] = prediction_result["ranking_confidence"]

            # Remove jax dependency from results.
            np_prediction_result = _jnp_to_np(dict(prediction_result))

            result_output_path = join(output_dir, f"result_{model_name}.pkl")
            with open(result_output_path, "wb") as f:
                pickle.dump(np_prediction_result, f, protocol=4)

            plddt_b_factors = np.repeat(
                plddt[:, None], residue_constants.atom_type_num, axis=-1
            )

            unrelaxed_protein = protein.from_prediction(
                features=processed_feature_dict,
                result=prediction_result,
                b_factors=plddt_b_factors,
                remove_leading_feature_dimension=not model_runner.multimer_mode,
            )

            unrelaxed_proteins[model_name] = unrelaxed_protein
            unrelaxed_pdbs[model_name] = protein.to_pdb(unrelaxed_protein)
            unrelaxed_pdb_path = join(output_dir, f"unrelaxed_{model_name}.pdb")
            with open(unrelaxed_pdb_path, "w") as f:
                f.write(unrelaxed_pdbs[model_name])


        # Rank by model confidence.
        ranked_order = [
            model_name
            for model_name, confidence in sorted(
                ranking_confidences.items(), key=lambda x: x[1], reverse=True
            )
        ]

        # Relax predictions.
        amber_relaxer = relax.AmberRelaxation(
            max_iterations=run_alphafold.RELAX_MAX_ITERATIONS,
            tolerance=run_alphafold.RELAX_ENERGY_TOLERANCE,
            stiffness=run_alphafold.RELAX_STIFFNESS,
            exclude_residues=run_alphafold.RELAX_EXCLUDE_RESIDUES,
            max_outer_iterations=run_alphafold.RELAX_MAX_OUTER_ITERATIONS,
            use_gpu=use_gpu_relax,
        )

        to_relax = []
        if models_to_relax == ModelsToRelax.BEST:
            to_relax = [ranked_order[0]]
        elif models_to_relax == ModelsToRelax.ALL:
            to_relax = ranked_order

        for model_name in to_relax:
            t_0 = time.time()
            relaxed_pdb_str, _, violations = amber_relaxer.process(
                prot=unrelaxed_proteins[model_name]
            )
            relax_metrics[model_name] = {
                "remaining_violations": violations,
                "remaining_violations_count": sum(violations),
            }
            timings[f"relax_{model_name}"] = time.time() - t_0

            relaxed_pdbs[model_name] = relaxed_pdb_str

            # Save the relaxed PDB.
            relaxed_output_path = join(output_dir, f"relaxed_{model_name}.pdb")
            with open(relaxed_output_path, "w") as f:
                f.write(relaxed_pdb_str)

        # Write out relaxed PDBs in rank order.
        for idx, model_name in enumerate(ranked_order):
            ranked_output_path = join(output_dir, f"ranked_{idx}.pdb")
            with open(ranked_output_path, "w") as f:
                if model_name in relaxed_pdbs:
                    model = relaxed_pdbs[model_name]
                else:
                    model = unrelaxed_pdbs[model_name]
                f.write(model)

        if not exists(ranking_output_path):  # already exists if restored.
            with open(ranking_output_path, "w") as f:
                label = "iptm+ptm" if "iptm" in prediction_result else "plddts"
                f.write(
                    json.dumps(
                        {label: ranking_confidences, "order": ranked_order}, indent=4
                    )
                )

        timings_output_path = join(output_dir, "timings.json")
        with open(timings_output_path, "w") as f:
            f.write(json.dumps(timings, indent=4))
        if models_to_relax != ModelsToRelax.NONE:
            relax_metrics_path = join(output_dir, "relax_metrics.json")
            with open(relax_metrics_path, "w") as f:
                f.write(json.dumps(relax_metrics, indent=4))


    @staticmethod
    def postprocess(
        multimer: MultimericObject,
        output_path: str,
        zip_pickles: bool = False,
        remove_pickles: bool = False,
        **kwargs: Dict,
    ) -> None:
        create_and_save_pae_plots(multimer, output_path)
        post_prediction_process(
            output_path,
            zip_pickles=zip_pickles,
            remove_pickles=remove_pickles,
        )
