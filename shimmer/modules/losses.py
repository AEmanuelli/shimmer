from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import TypedDict

import torch
import torch.nn.functional as F

from shimmer.modules.contrastive_loss import ContrastiveLossType, VarContrastiveLossType
from shimmer.modules.domain import DomainModule, LossOutput
from shimmer.modules.gw_module import GWModule, GWModuleBase, VariationalGWModule
from shimmer.modules.vae import kl_divergence_loss

LatentsDomainGroupT = Mapping[str, torch.Tensor]
LatentsT = Mapping[frozenset[str], LatentsDomainGroupT]


class GWLossesBase(torch.nn.Module, ABC):
    """
    Base Abstract Class for Global Workspace (GW) losses. This module is used
    to compute the different losses of the GW (typically translation, cycle,
    demi-cycle, contrastive losses).
    """

    @abstractmethod
    def step(self, domain_latents: LatentsT, mode: str) -> LossOutput:
        """
        Computes the losses
        Args:
            domain_latents: All latent groups
            mode: train/val/test
        Returns: LossOutput object
        """
        ...


def _demi_cycle_loss(
    gw_mod: GWModuleBase,
    domain_mods: dict[str, DomainModule],
    latent_domains: LatentsT,
) -> dict[str, torch.Tensor]:
    losses: dict[str, torch.Tensor] = {}
    metrics: dict[str, torch.Tensor] = {}
    for domains, latents in latent_domains.items():
        if len(domains) > 1:
            continue
        domain_name = next(iter(domains))
        domain_mod = domain_mods[domain_name]
        x_recons = gw_mod.decode(
            gw_mod.encode(gw_mod.on_before_gw_encode_dcy(latents)),
            domains={domain_name},
        )[domain_name]
        loss_output = domain_mod.compute_dcy_loss(x_recons, latents[domain_name])
        losses[f"demi_cycle_{domain_name}"] = loss_output.loss
        metrics.update(
            {f"demi_cycle_{domain_name}_{k}": v for k, v in loss_output.metrics.items()}
        )
    losses["demi_cycles"] = torch.stack(list(losses.values()), dim=0).mean()
    losses.update(metrics)
    return losses


def _cycle_loss(
    gw_mod: GWModuleBase,
    domain_mods: dict[str, DomainModule],
    latent_domains: LatentsT,
) -> dict[str, torch.Tensor]:
    losses: dict[str, torch.Tensor] = {}
    metrics: dict[str, torch.Tensor] = {}
    for domains_source, latents_source in latent_domains.items():
        if len(domains_source) > 1:
            continue
        domain_name_source = list(domains_source)[0]

        domain_mod = domain_mods[domain_name_source]
        z = gw_mod.encode(gw_mod.on_before_gw_encode_cy(latents_source))
        for domain_name_target in domain_mods.keys():
            if domain_name_target == domain_name_source:
                continue

            x_pred = gw_mod.decode(z, domains={domain_name_target})
            x_recons = gw_mod.decode(
                gw_mod.encode(x_pred), domains={domain_name_source}
            )

            loss_name = f"{domain_name_source}_through_{domain_name_target}"
            loss_output = domain_mod.compute_cy_loss(
                x_recons[domain_name_source],
                latents_source[domain_name_source],
            )
            metrics.update(
                {f"cycle_{loss_name}_{k}": v for k, v in loss_output.metrics.items()}
            )
            losses[f"cycle_{loss_name}"] = loss_output.loss
    losses["cycles"] = torch.stack(list(losses.values()), dim=0).mean()
    losses.update(metrics)
    return losses


def _translation_loss(
    gw_mod: GWModuleBase,
    domain_mods: dict[str, DomainModule],
    latent_domains: LatentsT,
) -> dict[str, torch.Tensor]:
    losses: dict[str, torch.Tensor] = {}
    metrics: dict[str, torch.Tensor] = {}
    for domains, latents in latent_domains.items():
        if len(domains) < 2:
            continue
        for domain_name_target in domains:

            domain_sources = {
                domain: latents[domain]
                for domain in domains
                if domain != domain_name_target
            }

            z = gw_mod.encode(gw_mod.on_before_gw_encode_tr(domain_sources))
            mod = domain_mods[domain_name_target]

            domain_source_names = "/".join(domain_sources.keys())
            loss_name = f"{domain_source_names}_to_{domain_name_target}"
            if loss_name in losses.keys():
                raise ValueError(f"{loss_name} is already computed.")

            prediction = gw_mod.decode(z, domains={domain_name_target})[
                domain_name_target
            ]
            loss_output = mod.compute_tr_loss(
                prediction,
                latents[domain_name_target],
            )
            losses[f"translation_{loss_name}"] = loss_output.loss
            metrics.update(
                {
                    f"translation_{loss_name}_{k}": v
                    for k, v in loss_output.metrics.items()
                }
            )
    losses["translations"] = torch.stack(list(losses.values()), dim=0).mean()
    losses.update(metrics)
    return losses


def _contrastive_loss(
    gw_mod: GWModuleBase,
    latent_domains: LatentsT,
    contrastive_fn: ContrastiveLossType,
) -> dict[str, torch.Tensor]:
    losses: dict[str, torch.Tensor] = {}
    metrics: dict[str, torch.Tensor] = {}
    keys: list[set[str]] = []

    for latents in latent_domains.values():
        if len(latents) != 2:
            continue
        for domain1_name, domain1 in latents.items():
            z1 = gw_mod.encode(gw_mod.on_before_gw_encode_cont({domain1_name: domain1}))
            for domain2_name, domain2 in latents.items():
                selected_domains = {domain1_name, domain2_name}
                if domain1_name == domain2_name or selected_domains in keys:
                    continue

                keys.append(selected_domains)

                loss_name = f"contrastive_{domain1_name}_and_{domain2_name}"
                z2 = gw_mod.encode(
                    gw_mod.on_before_gw_encode_cont({domain2_name: domain2})
                )
                loss_output = contrastive_fn(z1, z2)
                losses[loss_name] = loss_output.loss
                metrics.update(
                    {f"{loss_name}_{k}": v for k, v in loss_output.metrics.items()}
                )

    losses["contrastives"] = torch.stack(list(losses.values()), dim=0).mean()
    losses.update(metrics)
    return losses


def _contrastive_loss_with_uncertainty(
    gw_mod: VariationalGWModule,
    latent_domains: LatentsT,
    contrastive_fn: VarContrastiveLossType,
) -> dict[str, torch.Tensor]:
    losses: dict[str, torch.Tensor] = {}
    metrics: dict[str, torch.Tensor] = {}
    keys: list[set[str]] = []

    for latents in latent_domains.values():
        if len(latents) < 2:
            continue
        for domain1_name, domain1 in latents.items():
            z1_mean, z1_log_uncertainty = gw_mod.encoded_distribution(
                gw_mod.on_before_gw_encode_cont({domain1_name: domain1})
            )
            for domain2_name, domain2 in latents.items():
                selected_domains = {domain1_name, domain2_name}
                if domain1_name == domain2_name or selected_domains in keys:
                    continue

                keys.append(selected_domains)

                loss_name = f"contrastive_{domain1_name}_and_{domain2_name}"
                z2_mean, z2_log_uncertainty = gw_mod.encoded_distribution(
                    gw_mod.on_before_gw_encode_cont({domain2_name: domain2})
                )
                loss_output = contrastive_fn(
                    z1_mean[domain1_name],
                    z1_log_uncertainty[domain1_name],
                    z2_mean[domain2_name],
                    z2_log_uncertainty[domain2_name],
                )
                losses[loss_name] = loss_output.loss
                metrics.update(
                    {f"{loss_name}_{k}": v for k, v in loss_output.metrics.items()}
                )

    losses["contrastives"] = torch.stack(list(losses.values()), dim=0).mean()
    losses.update(metrics)
    return losses


class LossCoefs(TypedDict, total=False):
    """
    Dict of loss coefficients used in the GlobalWorkspace
    If one is not provided, the coefficient is assumed to be 0 and will not be logged.
    If the loss is excplicitely set to 0, it will be logged, but not take part in
    the total loss.
    """

    demi_cycles: float
    cycles: float
    translations: float
    contrastives: float


class GWLosses(GWLossesBase):
    def __init__(
        self,
        gw_mod: GWModule,
        domain_mods: dict[str, DomainModule],
        loss_coefs: LossCoefs,
        contrastive_fn: ContrastiveLossType,
    ):
        """
        Main loss module to use with the GlobalWorkspace
        Args:
            gw_mod: the GWModule
            domain_mods: a dict where the key is the domain name and
                value is the DomainModule
            loss_coefs: loss coefficients. LossCoefs object, or a mapping to float with
                correct keys.
            contrastive_fn: the contrastive function to use in contrastive loss
        """

        super().__init__()
        self.gw_mod = gw_mod
        self.domain_mods = domain_mods
        self.loss_coefs = loss_coefs
        self.contrastive_fn = contrastive_fn

    def demi_cycle_loss(self, latent_domains: LatentsT) -> dict[str, torch.Tensor]:
        return _demi_cycle_loss(self.gw_mod, self.domain_mods, latent_domains)

    def cycle_loss(self, latent_domains: LatentsT) -> dict[str, torch.Tensor]:
        return _cycle_loss(self.gw_mod, self.domain_mods, latent_domains)

    def translation_loss(self, latent_domains: LatentsT) -> dict[str, torch.Tensor]:
        return _translation_loss(self.gw_mod, self.domain_mods, latent_domains)

    def contrastive_loss(self, latent_domains: LatentsT) -> dict[str, torch.Tensor]:
        return _contrastive_loss(self.gw_mod, latent_domains, self.contrastive_fn)

    def step(
        self, domain_latents: Mapping[frozenset[str], Mapping[str, torch.Tensor]], _
    ) -> LossOutput:
        metrics: dict[str, torch.Tensor] = {}

        metrics.update(self.demi_cycle_loss(domain_latents))
        metrics.update(self.cycle_loss(domain_latents))
        metrics.update(self.translation_loss(domain_latents))
        metrics.update(self.contrastive_loss(domain_latents))

        loss = torch.stack(
            [
                metrics[name] * coef
                for name, coef in self.loss_coefs.items()
                if isinstance(coef, float) and coef > 0
            ],
            dim=0,
        ).mean()

        return LossOutput(loss, metrics)


class VariationalLossCoefs(LossCoefs, total=False):
    kl: float


class VariationalGWLosses(GWLossesBase):
    def __init__(
        self,
        gw_mod: VariationalGWModule,
        domain_mods: dict[str, DomainModule],
        loss_coefs: VariationalLossCoefs,
        contrastive_fn: ContrastiveLossType | None = None,
        var_contrastive_fn: VarContrastiveLossType | None = None,
    ):
        """
        Variational loss module to use with the VariationalGlobalWorkspace
        Args:
            gw_mod: the GWModule
            domain_mods: a dict where the key is the domain name and
                value is the DomainModule
            loss_coefs: loss coefficients. LossCoefs object, or a mapping to float with
                correct keys.
            contrastive_fn: the contrastive function to use in contrastive loss
            var_contrastive_fn: a contrastive function that uses uncertainty
        """

        super().__init__()

        self.gw_mod = gw_mod
        self.domain_mods = domain_mods
        self.loss_coefs = loss_coefs
        assert (contrastive_fn is not None) != (
            var_contrastive_fn is not None
        ), "Should either have contrastive_fn or var_contrastive_fn"
        self.contrastive_fn = contrastive_fn
        self.var_contrastive_fn = var_contrastive_fn

    def demi_cycle_loss(self, latent_domains: LatentsT) -> dict[str, torch.Tensor]:
        return _demi_cycle_loss(self.gw_mod, self.domain_mods, latent_domains)

    def cycle_loss(self, latent_domains: LatentsT) -> dict[str, torch.Tensor]:
        return _cycle_loss(self.gw_mod, self.domain_mods, latent_domains)

    def translation_loss(self, latent_domains: LatentsT) -> dict[str, torch.Tensor]:
        return _translation_loss(self.gw_mod, self.domain_mods, latent_domains)

    def contrastive_loss(self, latent_domains: LatentsT) -> dict[str, torch.Tensor]:
        if self.var_contrastive_fn is not None:
            return _contrastive_loss_with_uncertainty(
                self.gw_mod, latent_domains, self.var_contrastive_fn
            )

        assert self.contrastive_fn is not None
        return _contrastive_loss(self.gw_mod, latent_domains, self.contrastive_fn)

    def kl_loss(self, latent_domains: LatentsT) -> dict[str, torch.Tensor]:
        losses: dict[str, torch.Tensor] = {}

        for domains, latents in latent_domains.items():
            if len(domains) > 1:
                continue
            for domain_name in domains:
                mean, logvar = self.gw_mod.encoded_distribution(
                    {domain_name: latents[domain_name]}
                )
                loss_name = f"kl_{domain_name}"
                norm = mean[domain_name].size(0) + mean[domain_name].size(1)
                losses[loss_name] = (
                    kl_divergence_loss(mean[domain_name], logvar[domain_name]) / norm
                )
        losses["kl"] = torch.stack(list(losses.values()), dim=0).mean()
        return losses

    def step(
        self, domain_latents: Mapping[frozenset[str], Mapping[str, torch.Tensor]], _
    ) -> LossOutput:
        metrics: dict[str, torch.Tensor] = {}

        dcy_losses = self.demi_cycle_loss(domain_latents)
        metrics.update(dcy_losses)
        cy_losses = self.cycle_loss(domain_latents)
        metrics.update(cy_losses)
        tr_losses = self.translation_loss(domain_latents)
        metrics.update(tr_losses)
        cont_losses = self.contrastive_loss(domain_latents)
        metrics.update(cont_losses)
        kl_losses = self.kl_loss(domain_latents)
        metrics.update(kl_losses)

        loss = torch.stack(
            [
                metrics[name] * coef
                for name, coef in self.loss_coefs.items()
                if isinstance(coef, float) and coef > 0
            ],
            dim=0,
        ).mean()

        return LossOutput(loss, metrics)


def sample_scaling_factors(
    binary_scaling_prob: float,
    batch_size: int,
    temperature: float,
    device: torch.device,
):
    """
    Args:
        binary_scaling_prob: float
        batch_size: int
        temperature: float greater than 0
    """
    assert 0 <= binary_scaling_prob <= 1

    # TODO: make selection deterministic
    binary_mask = torch.rand(batch_size) < binary_scaling_prob

    binary_factors = torch.randint(0, 2, (batch_size,)).float()
    binary_softmax = torch.stack([binary_factors, 1 - binary_factors], dim=1)

    uniform_samples = torch.rand(batch_size)
    uniform_for_softmax = torch.stack([uniform_samples, 1 - uniform_samples], dim=1)

    uniform_softmax = F.softmax(uniform_for_softmax * temperature, dim=1)

    scaling_factors = torch.where(
        binary_mask.unsqueeze(-1), binary_softmax, uniform_softmax
    ).to(device)

    binary_indices = torch.where(binary_mask)[0]
    softmax_indices = torch.where(~binary_mask)[0]

    binary_scaling_factors = scaling_factors[binary_indices]
    softmax_scaling_factors = scaling_factors[softmax_indices]

    return {
        "binary": (
            binary_scaling_factors[:, 0],
            binary_scaling_factors[:, 1],
            binary_indices,
        ),
        "softmax": (
            softmax_scaling_factors[:, 0],
            softmax_scaling_factors[:, 1],
            softmax_indices,
        ),
    }


class GWLossesFusion(GWLossesBase):
    def __init__(
        self,
        gw_mod: GWModule,
        domain_mods: dict[str, DomainModule],
        contrastive_fn: ContrastiveLossType,
    ):
        super().__init__()
        self.gw_mod = gw_mod
        self.domain_mods = domain_mods
        self.contrastive_fn = contrastive_fn

    def demi_cycle_loss(self, latent_domains: LatentsT) -> dict[str, torch.Tensor]:
        return _demi_cycle_loss(self.gw_mod, self.domain_mods, latent_domains)

    def cycle_loss(self, latent_domains: LatentsT) -> dict[str, torch.Tensor]:
        return _cycle_loss(self.gw_mod, self.domain_mods, latent_domains)

    def translation_loss(self, latent_domains: LatentsT) -> dict[str, torch.Tensor]:
        return _translation_loss(self.gw_mod, self.domain_mods, latent_domains)

    def contrastive_loss(self, latent_domains: LatentsT) -> dict[str, torch.Tensor]:
        return _contrastive_loss(self.gw_mod, latent_domains, self.contrastive_fn)

    def broadcast_loss(
        self, latent_domains: LatentsT, mode: str
    ) -> dict[str, torch.Tensor]:
        losses: dict[str, torch.Tensor] = {}
        metrics: dict[str, torch.Tensor] = {}

        for latents in latent_domains.values():
            if len(latents) < 2:
                continue
            batch_size = latents[next(iter(latents))].size(0)
            device = latents[next(iter(latents))].device

            if mode == "val":
                scaling_factors = sample_scaling_factors(0.5, batch_size, 5.0, device)
            else:
                scaling_factors = sample_scaling_factors(0.0, batch_size, 5.0, device)

            for scale_type, (
                scaling_factor_1,
                scaling_factor_2,
                indices,
            ) in scaling_factors.items():
                scaled_latents = {}

                for i, (domain_name, latent) in enumerate(latents.items()):
                    scaling_factor = scaling_factor_1 if i == 0 else scaling_factor_2
                    scaled_latents_subset = latent[indices] * scaling_factor.unsqueeze(
                        -1
                    )
                    scaled_latents_subset = scaled_latents_subset.to(latent)

                    scaled_latents[domain_name] = scaled_latents_subset

                encoded_latents_for_subset = self.gw_mod.encode(scaled_latents)
                encoded_latents_for_subset = torch.tanh(encoded_latents_for_subset)
                decoded_latents_for_subset = self.gw_mod.decode(
                    encoded_latents_for_subset
                )

                for domain_name, latent in latents.items():
                    domain_mod = self.domain_mods[domain_name]
                    decoded_latent_for_domain_subset = decoded_latents_for_subset[
                        domain_name
                    ]
                    original_latent_for_domain_subset = latents[domain_name][indices]
                    loss_output = domain_mod.compute_broadcast_loss(
                        decoded_latent_for_domain_subset,
                        original_latent_for_domain_subset,
                    )
                    loss_key = f"{domain_name}_loss_{scale_type}"

                    metrics.update(
                        {
                            f"broadcast_{loss_key}_{k}": v
                            for k, v in loss_output.metrics.items()
                        }
                    )
                    losses[loss_key] = loss_output.loss.mean()

            binary_count = scaling_factors["binary"][2].size(0)
            softmax_count = scaling_factors["softmax"][2].size(0)
            total_count = binary_count + softmax_count

            for domain_name, latent in latents.items():
                full_loss_key = f"{domain_name}_full_loss"

                binary_loss_key = f"{domain_name}_loss_binary"
                softmax_loss_key = f"{domain_name}_loss_softmax"

                binary_loss = losses[binary_loss_key] * (binary_count / total_count)
                softmax_loss = losses[softmax_loss_key] * (softmax_count / total_count)

                losses[full_loss_key] = binary_loss + softmax_loss

        losses["broadcast"] = torch.stack(
            [loss for name, loss in losses.items() if "full_loss" in name], dim=0
        ).mean()
        losses.update(metrics)
        return losses

    def step(
        self,
        domain_latents: Mapping[frozenset[str], Mapping[str, torch.Tensor]],
        mode: str,
    ) -> LossOutput:
        metrics: dict[str, torch.Tensor] = {}

        metrics.update(self.demi_cycle_loss(domain_latents))
        metrics.update(self.cycle_loss(domain_latents))
        metrics.update(self.translation_loss(domain_latents))
        metrics.update(self.contrastive_loss(domain_latents))
        metrics.update(self.broadcast_loss(domain_latents, mode))

        loss = metrics["broadcast"]

        return LossOutput(loss, metrics)
