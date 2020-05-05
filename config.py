import re
import sys
import string
import logging
import pathlib
import itertools

import numpy as np
import toml

import sim
import vcf


class _CLIFormatter(logging.Formatter):
    def format(self, record):
        if record.levelno == logging.INFO:
            self._style = logging.PercentStyle("%(message)s")
        else:
            self._style = logging.PercentStyle("%(levelname)s: %(message)s")
        if record.levelno == logging.WARNING and len(record.args) > 0:
            # trim the ugly warnings.warn message
            match = re.search(
                    r"Warning:\s*(.*?)\s*warnings.warn\(", record.args[0], re.DOTALL)
            if match is not None:
                record.args = (match.group(1),)
        return super().format(record)


def logger_setup(level):
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_CLIFormatter())
    logging.basicConfig(handlers=[handler], level=level)
    logging.captureWarnings(True)


class ConfigError(RuntimeError):
    pass


class Config():
    def __init__(self, filename):

        # Attributes for external use.
        self.filename = filename
        self.dir = None
        self.ref_pop = None
        self.num_rows = None
        self.num_cols = None
        self.pop = None
        self.sequence_length = None
        self.min_allele_frequency = None
        self.vcf_samples = None
        self.vcf_tranche = None
        self.phasing = None
        self.pop2tsidx = None
        self.tf_devices = None

        # Read in toml file and fill in the attributes.
        self.config = toml.load(self.filename)
        self._getcfg_toplevel()
        self._getcfg_pop()
        self._getcfg_sim()
        self._getcfg_vcf()
        self._getcfg_train()

    def _verify_keys_exist(self, dict_, keys, pfx=""):
        for k in keys:
            if k not in dict_:
                raise ConfigError(f"{self.filename}: {pfx}{k} not defined.")

    def _getcfg_toplevel(self):
        toplevel_keys = ["dir", "ref_pop", "pop", "sim", "vcf", "train"]
        self._verify_keys_exist(self.config, toplevel_keys)
        self.dir = pathlib.Path(self.config["dir"])
        self.ref_pop = self.config["ref_pop"]

    def _getcfg_pop(self):
        pop = self.config.get("pop")
        if len(pop) == 0:
            raise ConfigError(f"{self.filename}: no populations defined.")
        for k, v in pop.items():
            if not isinstance(v, list):
                pop[k] = []
                with open(v) as f:
                    for line in f:
                        pop[k].append(line.strip())
        if self.ref_pop not in pop:
            raise ConfigError(
                    f"{self.filename}: ref_pop {self.ref_pop} not among those "
                    f"to be used for the genotype matrix: {pop}.")
        self.pop = pop
        self.vcf_samples = list(itertools.chain(*self.pop.values()))
        # number of haplotypes
        self.num_cols = 2 * len(self.vcf_samples)

    def _getcfg_sim(self):
        self.sim = self.config.get("sim")
        if len(self.sim) == 0:
            raise ConfigError(f"{self.filename}: no simulations defined.")
        sim_keys = ["sequence_length", "min_allele_frequency", "tranche"]
        self._verify_keys_exist(self.sim, sim_keys, "sim.")
        self.sequence_length = self.sim["sequence_length"]
        self.min_allele_frequency = self.sim["min_allele_frequency"]
        self._getcfg_tranche(self.sim["tranche"])

    def _getcfg_tranche(self, tranche):
        assert tranche is not None
        if len(tranche) == 0:
            raise ConfigError(f"{self.filename}: no tranches defined.")
        for label, speclist in tranche.items():
            for modelspec in speclist:
                model = sim.get_demog_model(modelspec)
                pop2tsidx = {p.id: i for i, p in enumerate(model.populations)}
                if self.pop2tsidx is None:
                    self.pop2tsidx = pop2tsidx
                elif pop2tsidx.items() != self.pop2tsidx.items():
                    raise ConfigError(
                            f"{self.filename} populations defined for {modelspec} "
                            "do not match earlier modelspecs.")
                for pop in self.pop.keys():
                    if pop not in pop2tsidx:
                        raise ConfigError(
                                f"{self.filename}: {pop} not found in {modelspec}. "
                                f"Options are: {list(pop2tsidx.keys())}")
        self.tranche = tranche

    def _getcfg_vcf(self):
        self.vcf = self.config.get("vcf")
        if len(self.vcf) == 0:
            raise ConfigError(f"{self.filename}: no vcf defined.")
        vcf_keys = ["file", ]
        self._verify_keys_exist(self.vcf, vcf_keys, "vcf.")
        file_ = self.vcf.get("file")
        chr_ = self.vcf.get("chr")
        if chr_ is None:
            self.file = [pathlib.Path(file_)]
        else:
            self.file = []
            for c in chr_:
                fn = string.Template(file_).substitute(chr=c)
                self.file.append(pathlib.Path(fn))
        for fn in self.file:
            if not fn.exists():
                raise ConfigError(f"{fn}: file not found.")
        self.phasing = vcf.sample_phasing(self.file[0], self.vcf_samples)

    def _getcfg_train(self):
        train = self.config.get("train")
        if len(train) == 0:
            raise ConfigError(f"{self.filename}: no training details defined.")
        train_keys = ["num_rows", "epochs", "batch_size", "model"]
        self._verify_keys_exist(train, train_keys, "train.")
        self.num_rows = train.get("num_rows")
        self.train_epochs = train.get("epochs")
        self.train_batch_size = train.get("batch_size")
        self.nn_model = train.get("model")

        nn_model_keys = {
                "cnn": [
                        "n_conv", "n_conv_filt", "filt_size_x", "filt_size_y",
                        "n_dense", "dense_size"],
                }
        params = train.get(self.nn_model)
        if params is None or self.nn_model not in nn_model_keys:
            raise ConfigError(
                    f"{self.filename}: train.model must be set to one of: "
                    f"{list(nn_model_keys.keys())}")
        self._verify_keys_exist(
                params, nn_model_keys[self.nn_model], "train.{self.nn_model}")
        self.nn_model_params = params

    def _getcfg_tf(self):
        tf = self.config.get("tf")
        if tf is not None:
            self.tf_devices = tf.get("devices")

    def sample_counts(self):
        """
        Haploid sample numbers for each population.
        """
        return {p: 2*len(v) for p, v in self.pop.items()}

    def pop_indices(self):
        """
        Indices partitioning the populations in a genotype matrix.
        """
        indices = np.cumsum(list(self.sample_counts().values()))
        # Record the starting indices.
        indices = [0] + list(indices[:-1])
        return dict(zip(self.pop.keys(), indices))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} config.toml")
    cfg_file = sys.argv[1]
    cfg = Config(cfg_file)
    print(cfg.sample_counts())
    print(cfg.phasing.count(True), cfg.phasing.count(False))