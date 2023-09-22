from collections.abc import Iterable, Mapping
from typing import Any, TypedDict, cast

import torch
from lightning.pytorch import LightningModule
from torch.nn import ModuleDict
from torch.optim.lr_scheduler import OneCycleLR

from shimmer.modules.domain import DomainDescription, DomainModule
from shimmer.modules.gw_module import (
    DeterministicGWModule,
    GWModule,
    VariationalGWModule,
)
from shimmer.modules.losses import (
    DeterministicGWLosses,
    GWLosses,
    LatentsT,
    LossCoefs,
    VariationalGWLosses,
)


class SchedulerArgs(TypedDict, total=False):
    max_lr: float
    total_steps: int


class GlobalWorkspace(LightningModule):
    def __init__(
        self,
        gw_mod: GWModule,
        domain_mods: dict[str, DomainModule],
        loss_mod: GWLosses,
        optim_lr: float = 1e-3,
        optim_weight_decay: float = 0.0,
        scheduler_args: SchedulerArgs | None = None,
    ) -> None:
        super().__init__()
        self.save_hyperparameters(
            ignore=["gw_mod", "domain_mods", "loss_mod", "domain_descriptions"]
        )

        self.gw_mod = gw_mod
        self.domain_mods = domain_mods
        self.loss_mod = loss_mod

        self.optim_lr = optim_lr
        self.optim_weight_decay = optim_weight_decay
        self.scheduler_args = SchedulerArgs(max_lr=optim_lr, total_steps=1)
        if scheduler_args is not None:
            self.scheduler_args.update(scheduler_args)

    def encode(self, x: Mapping[str, torch.Tensor]) -> torch.Tensor:
        return self.gw_mod.encode(x)

    def decode(
        self, z: torch.Tensor, domains: Iterable[str] | None = None
    ) -> dict[str, torch.Tensor]:
        return self.gw_mod.decode(z, domains)

    def batch_demi_cycles(
        self, latent_domains: LatentsT
    ) -> dict[str, torch.Tensor]:
        predictions: dict[str, torch.Tensor] = {}
        for domains, latents in latent_domains.items():
            if len(domains) > 1:
                continue
            domain_name = list(domains)[0]
            z = self.gw_mod.translate(latents, to=domain_name)
            predictions[domain_name] = z
        return predictions

    def batch_cycles(
        self, latent_domains: LatentsT
    ) -> dict[tuple[str, str], torch.Tensor]:
        predictions: dict[tuple[str, str], torch.Tensor] = {}
        for domains_source, latents_source in latent_domains.items():
            if len(domains_source) > 1:
                continue
            domain_name_source = next(iter(domains_source))
            for domain_name_target in self.domain_mods.keys():
                if domain_name_source == domain_name_target:
                    continue
                z = self.gw_mod.cycle(
                    latents_source, through=domain_name_target
                )
                domains = (domain_name_source, domain_name_target)
                predictions[domains] = z[domain_name_source]
        return predictions

    def batch_translations(
        self, latent_domains: LatentsT
    ) -> dict[tuple[str, str], torch.Tensor]:
        predictions: dict[tuple[str, str], torch.Tensor] = {}
        for domains, latents in latent_domains.items():
            if len(domains) < 2:
                continue
            for domain_name_source in domains:
                for domain_name_target in domains:
                    if domain_name_source == domain_name_target:
                        continue
                    prediction = self.gw_mod.translate(
                        {domain_name_source: latents[domain_name_source]},
                        to=domain_name_target,
                    )
                    predictions[
                        (domain_name_source, domain_name_target)
                    ] = prediction
        return predictions

    def encode_domain(self, domain: Any, name: str) -> torch.Tensor:
        return self.domain_mods[name].encode(domain)

    def encode_domains(
        self,
        batch: Mapping[frozenset[str], Mapping[str, Any]],
    ) -> dict[frozenset[str], dict[str, torch.Tensor]]:
        return {
            domains: {
                name: self.domain_mods[name].encode(domain)
                for name, domain in data.items()
            }
            for domains, data in batch.items()
        }

    def decode_domain(self, domain: torch.Tensor, name: str) -> Any:
        return self.domain_mods[name].decode(domain)

    def decode_domains(
        self,
        latents_domain: LatentsT,
    ) -> dict[frozenset[str], dict[str, Any]]:
        return {
            domains: {
                name: self.domain_mods[name].decode(domain)
                for name, domain in latents.items()
            }
            for domains, latents in latents_domain.items()
        }

    def _get_batch_size(
        self,
        domain_latents: LatentsT,
    ) -> int:
        for data in domain_latents.values():
            for tensor in data.values():
                return tensor.size(0)
        raise ValueError("Empty batch.")

    def generic_step(
        self,
        batch: Mapping[frozenset[str], Mapping[str, Any]],
        mode: str,
    ) -> torch.Tensor:
        domain_latents = self.encode_domains(batch)
        batch_size = self._get_batch_size(domain_latents)

        losses = self.loss_mod.step(domain_latents)

        for name, loss in losses.items():
            self.log(f"{mode}/{name}", loss, batch_size=batch_size)

        return losses["loss"]

    def validation_step(self, data: Mapping[str, Any], _) -> torch.Tensor:
        batch = {frozenset(data.keys()): data}
        for domain in data.keys():
            batch[frozenset([domain])] = {domain: data[domain]}
        return self.generic_step(batch, mode="val")

    def training_step(
        self, batch: Mapping[frozenset[str], Mapping[str, Any]], _
    ) -> torch.Tensor:
        return self.generic_step(batch, mode="train")

    def configure_optimizers(self) -> dict[str, Any]:
        optimizer = torch.optim.AdamW(
            self.parameters(),
            lr=self.optim_lr,
            weight_decay=self.optim_weight_decay,
        )

        lr_scheduler = OneCycleLR(optimizer, **self.scheduler_args)

        return {
            "optimizer": optimizer,
            "lr_scheduler": {
                "scheduler": lr_scheduler,
                "interval": "step",
            },
        }


class DeterministicGlobalWorkspace(GlobalWorkspace):
    def __init__(
        self,
        domain_descriptions: Mapping[str, DomainDescription],
        latent_dim: int,
        loss_coefs: LossCoefs,
        optim_lr: float = 1e-3,
        optim_weight_decay: float = 0.0,
        scheduler_args: SchedulerArgs | None = None,
    ) -> None:
        gw_mod = DeterministicGWModule(domain_descriptions, latent_dim)

        domain_mods = {
            name: domain.module for name, domain in domain_descriptions.items()
        }
        for mod in domain_mods.values():
            mod.freeze()
        domain_mods = cast(dict[str, DomainModule], ModuleDict(domain_mods))

        loss_mod = DeterministicGWLosses(gw_mod, domain_mods, loss_coefs)

        super().__init__(
            gw_mod,
            domain_mods,
            loss_mod,
            optim_lr,
            optim_weight_decay,
            scheduler_args,
        )


class VariationalGlobalWorkspace(GlobalWorkspace):
    def __init__(
        self,
        domain_descriptions: Mapping[str, DomainDescription],
        latent_dim: int,
        loss_coefs: LossCoefs,
        optim_lr: float = 1e-3,
        optim_weight_decay: float = 0.0,
        scheduler_args: SchedulerArgs | None = None,
    ) -> None:
        gw_mod = VariationalGWModule(domain_descriptions, latent_dim)

        domain_mods = {
            name: domain.module for name, domain in domain_descriptions.items()
        }
        for mod in domain_mods.values():
            mod.freeze()
        domain_mods = cast(dict[str, DomainModule], ModuleDict(domain_mods))

        loss_mod = VariationalGWLosses(gw_mod, domain_mods, loss_coefs)

        super().__init__(
            gw_mod,
            domain_mods,
            loss_mod,
            optim_lr,
            optim_weight_decay,
            scheduler_args,
        )
