import json
import logging
import os
from pathlib import Path

import torch

from decentralizepy.sharing.Sharing import Sharing


class PartialModel(Sharing):
    def __init__(
        self,
        rank,
        machine_id,
        communication,
        mapping,
        graph,
        model,
        dataset,
        log_dir,
        alpha=1.0,
        dict_ordered=True,
        save_shared=False,
    ):
        """
        Constructor
        Parameters
        ----------
        rank : int
            Local rank
        machine_id : int
            Global machine id
        communication : decentralizepy.communication.Communication
            Communication module used to send and receive messages
        mapping : decentralizepy.mappings.Mapping
            Mapping (rank, machine_id) -> uid
        graph : decentralizepy.graphs.Graph
            Graph reprensenting neighbors
        model : decentralizepy.models.Model
            Model to train
        dataset : decentralizepy.datasets.Dataset
            Dataset for sharing data. Not implemented yer! TODO
        log_dir : str
            Location to write shared_params (only writing for 2 procs per machine)
        """
        super().__init__(
            rank, machine_id, communication, mapping, graph, model, dataset
        )
        self.log_dir = log_dir
        self.alpha = alpha
        self.dict_ordered = dict_ordered
        self.save_shared = save_shared

        # Only save for 2 procs
        if rank == 0 or rank == 1:
            self.save_shared = True

        if self.save_shared:
            self.folder_path = os.path.join(
                self.log_dir, "shared_params/{}".format(self.rank)
            )
            Path(self.folder_path).mkdir(parents=True, exist_ok=True)

    def extract_top_gradients(self):
        logging.info("Summing up gradients")
        assert len(self.model.accumulated_gradients) > 0
        gradient_sum = self.model.accumulated_gradients[0]
        for i in range(1, len(self.model.accumulated_gradients)):
            for key in self.model.accumulated_gradients[i]:
                gradient_sum[key] += self.model.accumulated_gradients[i][key]

        logging.info("Returning topk gradients")
        tensors_to_cat = [v.data.flatten() for _, v in gradient_sum.items()]
        G_topk = torch.abs(torch.cat(tensors_to_cat, dim=0))
        return torch.topk(
            G_topk, round(self.alpha * G_topk.shape[0]), dim=0, sorted=False
        )

    def serialized_model(self):
        with torch.no_grad():
            _, G_topk = self.extract_top_gradients()

            if self.save_shared:
                shared_params = dict()
                shared_params["order"] = list(self.model.state_dict().keys())
                shapes = dict()
                for k, v in self.model.state_dict().items():
                    shapes[k] = list(v.shape)
                shared_params["shapes"] = shapes

                shared_params[self.communication_round] = G_topk.tolist()

                with open(
                    os.path.join(
                        self.folder_path,
                        "{}_shared_params.json".format(self.communication_round + 1),
                    ),
                    "w",
                ) as of:
                    json.dump(shared_params, of)

            logging.info("Extracting topk params")

            tensors_to_cat = [v.data.flatten() for v in self.model.parameters()]
            T = torch.cat(tensors_to_cat, dim=0)
            T_topk = T[G_topk]

            logging.info("Generating dictionary to send")

            m = dict()

            if not self.dict_ordered:
                raise NotImplementedError

            m["indices"] = G_topk.numpy().tolist()
            m["params"] = T_topk.numpy().tolist()

            assert len(m["indices"]) == len(m["params"])
            logging.info("Elements sending: {}".format(len(m["indices"])))

            logging.info("Generated dictionary to send")

            for key in m:
                m[key] = json.dumps(m[key])

            logging.info("Converted dictionary to json")

            return m

    def deserialized_model(self, m):
        with torch.no_grad():
            state_dict = self.model.state_dict()

            if not self.dict_ordered:
                raise NotImplementedError

            shapes = []
            lens = []
            tensors_to_cat = []
            for _, v in state_dict.items():
                shapes.append(v.shape)
                t = v.flatten()
                lens.append(t.shape[0])
                tensors_to_cat.append(t)

            T = torch.cat(tensors_to_cat, dim=0)
            index_tensor = torch.tensor(json.loads(m["indices"]))
            logging.debug("Original tensor: {}".format(T[index_tensor]))
            T[index_tensor] = torch.tensor(json.loads(m["params"]))
            logging.debug("Final tensor: {}".format(T[index_tensor]))
            start_index = 0
            for i, key in enumerate(state_dict):
                end_index = start_index + lens[i]
                state_dict[key] = T[start_index:end_index].reshape(shapes[i])
                start_index = end_index

            return state_dict
