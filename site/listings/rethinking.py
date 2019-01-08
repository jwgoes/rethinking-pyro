import matplotlib.pyplot as plt
import pandas as pd
import torch
from torch.distributions import biject_to, constraints

import pyro
import pyro.distributions as dist
import pyro.poutine as poutine
from pyro.contrib.autoguide import AutoDelta
from pyro.contrib.util import hessian
from pyro.infer import Trace_ELBO


class MAP(object):

    def __init__(self, name, model, start={}, **kwargs):
        self.name = name
        self._model = model
        self._guide = AutoDelta(model, prefix=name)
        self._kwargs = kwargs
        self._latent_shapes = {}
        self._latent_names = {}
        trace = poutine.util.prune_subsample_sites(poutine.trace(model).get_trace(**kwargs))
        self._params = trace.param_nodes
        self._obs = trace.observation_nodes[0]
        for latent, site in trace.nodes.items():
            if site["type"] == "sample" and not site["is_observed"]:
                if latent in start:
                    init = start[latent]
                else:
                    if isinstance(site["fn"], dist.Cauchy):
                        median = site["fn"].loc
                    elif isinstance(site["fn"], dist.HalfCauchy):
                        median = site["fn"].scale
                    else:
                        median = site["fn"].mean
                    transform = biject_to(site["fn"].support)
                    unconstrained = transform.inv(median)
                    init = transform(unconstrained
                                     + torch.empty(unconstrained.shape).uniform_(-2, 2))
                    init = site["value"]
                pyro.param("{}_{}".format(name, latent), init, site["fn"].support)
                self._latent_shapes[latent] = site["value"].shape
                self._latent_names[latent] = latent
            elif site["type"] == "param":
                self._latent_shapes[latent.replace("{}_".format(name), "")] = site["value"].shape
                self._latent_names[latent] = latent

    def fit(self, lr=0.1, max_iter=500):
        elbo = Trace_ELBO()
        with poutine.trace(param_only=True) as param_capture:
            elbo.loss_and_grads(self._model, self._guide, **self._kwargs)
        params = set(site["value"].unconstrained() for site in param_capture.trace.nodes.values())
        optim = torch.optim.LBFGS(params, lr, max_iter)

        def closure():
            pyro.infer.util.zero_grads(params)
            return elbo.loss_and_grads(self._model, self._guide, **self._kwargs)

        optim.step(closure)

    def _coef(self, detach=True, guide_trace=None, model_trace=None):
        if guide_trace is None:
            guide_trace = poutine.trace(self._guide).get_trace(**self._kwargs)
        if model_trace is None:
            model_trace = poutine.trace(
                poutine.replay(self._model, trace=guide_trace)).get_trace(**self._kwargs)
        coefs = {}
        for latent in self._latent_shapes:
            param_name = "{}_{}".format(self.name, latent)
            if param_name in guide_trace.nodes:
                coef = guide_trace.nodes[param_name]["value"]
                coefs[latent] = coef.detach() if detach else coef
            elif param_name in model_trace.nodes:
                coef = model_trace.nodes[param_name]["value"]
                coefs[latent] = coef.detach() if detach else coef
        return coefs

    def coef(self):
        return self._coef()

    def log_lik(self, detach=True, trace=False):
        guide_trace = poutine.trace(self._guide).get_trace(**self._kwargs)
        model_trace = poutine.trace(
            poutine.replay(self._model, trace=guide_trace)).get_trace(**self._kwargs)
        loss = -model_trace.log_prob_sum()
        loss = loss.detach() if detach else loss
        return (loss, guide_trace, model_trace) if trace else loss

    def _vcov(self):
        loss, guide_trace, model_trace = self.log_lik(detach=False, trace=True)
        coefs = self._coef(detach=False, guide_trace=guide_trace, model_trace=model_trace)
        H = hessian(loss, coefs.values())
        return torch.inverse(H)

    def vcov(self):
        return self._vcov()

    def precis(self, prob=0.89, corr=False, digits=2):
        packed_mean = torch.cat([c.reshape(-1) for c in self.coef().values()])
        cov = self.vcov()
        packed_std_dev = cov.diag().sqrt()
        packed_quantiles = dist.Normal(packed_mean, packed_std_dev).icdf(
            torch.tensor([[(1 - prob) / 2], [(1 + prob) / 2]]))

        mean, std_dev, lower_quantile, upper_quantile = {}, {}, {}, {}
        pos = 0
        for latent, shape in self._latent_shapes.items():
            numel = shape.numel()
            for i in range(numel):
                name = "{}[{}]".format(latent, i) if numel > 1 else latent
                mean[name] = packed_mean[pos].item()
                std_dev[name] = packed_std_dev[pos].item()
                lower_quantile[name] = packed_quantiles[0, pos].item()
                upper_quantile[name] = packed_quantiles[1, pos].item()
                pos = pos + 1

        precis_dict = {"Mean": mean, "StdDev": std_dev,
                       "{:.1f}%".format((1 - prob) * 50): lower_quantile,
                       "{:.1f}%".format((1 + prob) * 50): upper_quantile}
        precis = pd.DataFrame.from_dict(precis_dict)
        if corr:
            corr_matrix = cov / (packed_std_dev.ger(packed_std_dev))
            corr_dict = {}
            pos = 0
            for latent, shape in self._latent_shapes.items():
                numel = shape.numel()
                for i in range(numel):
                    name = "{}[{}]".format(latent, i) if numel > 1 else latent
                    corr_dict[name] = corr_matrix[:, pos].tolist()
                    pos = pos + 1
            precis = precis.assign(**corr_dict)
        return precis.astype("float").round(digits).loc[mean.keys(), :]

    def precis_plot(self, **kwargs):
        precis = self.precis(**kwargs)
        plt.plot(precis["Mean"], precis.index, "ko", fillstyle="none")
        plt.xlabel("Value")
        for latent in precis.index:
            plt.plot(precis.loc[latent, ["5.5%", "94.5%"]], [latent, latent], c="k", lw=1.5)
        plt.gca().invert_yaxis()
        plt.axvline(x=0, c="k", alpha=0.2)
        plt.grid(axis="y", linestyle="--")

    def _extract_samples(self, n=10000):
        coefs_matrix = dist.MultivariateNormal(
            torch.cat([c.reshape(-1) for c in self._coef().values()]),
            self._vcov()).sample(torch.Size([n]))
        samples = {}
        pos = 0
        for latent, shape in self._latent_shapes.items():
            numel = shape.numel()
            samples[latent] = coefs_matrix[:, pos:(pos + numel)].reshape((n,) + shape)
            pos = pos + numel
        return samples

    def extract_samples(self, n=10000):
        return self._extract_samples(n)

    def _sim(self, n=1000, ret="sim", **kwargs):
        factors = self._kwargs.copy()
        for f in factors:
            if f in kwargs:
                factors[f] = kwargs[f]
        coefs = self._extract_samples(n)
        r = []
        link_shape = (list(kwargs.values())[-1].shape
                      if kwargs else list(self._kwargs.values())[-1].shape)
        for i in range(n):
            lifted = poutine.lift(self._model, dist.Normal(0, 1))
            conditioned = pyro.condition(lifted, data={self._latent_names[c]: value[i]
                                                       for c, value in coefs.items()})
            trace = poutine.trace(conditioned).get_trace(**factors)
            if ret == "link":
                r.append(trace.nodes[self._obs]["fn"].mean.expand(link_shape))
            elif ret == "sim":
                r.append(trace.nodes[self._obs]["fn"].sample().expand(link_shape))
            elif ret == "ll":
                r.append(trace.nodes[self._obs]["fn"].log_prob(trace.nodes[self._obs]["value"]))
        return torch.stack(r)

    def link(self, n=1000, **kwargs):
        return self._sim(n, "link", **kwargs)

    def sim(self, n=1000, **kwargs):
        return self._sim(n, "sim", **kwargs)

    def WAIC(self, n=1000, pointwise=False):
        ll = self._sim(n, "ll")
        lppd = torch.logsumexp(ll, 0) - torch.tensor(float(n)).log()
        pWAIC = ll.var(0)
        waic_vec = -2 * (lppd - pWAIC)
        WAIC = waic_vec.sum()
        se = (waic_vec.size(0) * waic_vec.var()).sqrt()
        info = {"WAIC": WAIC, "lppd": lppd.sum(), "pWAIC": pWAIC.sum(), "se": se}
        if pointwise:
            info["WAIC_vec"] = waic_vec
        return info


class LM(MAP):

    def __init__(self, name, start={}, **kwargs):
        self._fit_intercept = True if kwargs.pop("intercept", 1) == 1 else False
        self._kwargs_mean = torch.stack([kwargs[factor].mean() for factor in kwargs])
        for latent in start:
            pyro.param("{}_{}".format(name, latent), start[latent])
        super(LM, self).__init__(name, self._model, **kwargs)

    def _model(self, **kwargs):
        y_pred = 0
        if self._fit_intercept:
            y_pred = pyro.param("{}_intercept".format(self.name), lambda: self._kwargs_mean[-1])
        for i, (factor, value) in enumerate(kwargs.items()):
            if i == len(kwargs) - 1:
                sigma = pyro.param("{}_sigma".format(self.name), lambda: value.std(),
                                   constraints.positive)
                with pyro.plate("plate"):
                    return pyro.sample(factor, dist.Normal(y_pred, sigma), obs=value)
            coef = pyro.param("{}_{}".format(self.name, factor), lambda: torch.tensor(0.))
            y_pred = y_pred + coef * (value - self._kwargs_mean[i])

    def coef(self):
        coefs = self._coef()
        coefs["intercept"] = coefs["intercept"] - torch.stack(
            list(coefs.values())[1:-1]).dot(self._kwargs_mean[:-1])
        return coefs

    def vcov(self):
        vcov = self._vcov()
        transform = torch.eye(vcov.size(0))
        transform[0, 1:-1] = self._kwargs_mean[:-1]
        return transform.matmul(vcov).matmul(transform.t())

    def extract_samples(self, n=10000):
        samples = self._extract_samples(n)
        samples["intercept"] = samples["intercept"] - torch.stack(
            list(samples.values())[1:-1], dim=1).matmul(self._kwargs_mean[:-1])
        return samples


def dens(samples, adj=0.5, plot=True, c=None, lw=None, xlab=None):
    bw_factor = (0.75 * samples.size(0)) ** (-0.2)
    bw = adj * bw_factor * samples.std()
    x_min = samples.min()
    x_max = samples.max()
    x = torch.linspace(x_min, x_max, 1000)
    y = dist.Normal(samples, bw).log_prob(x.unsqueeze(-1)).logsumexp(-1).exp()
    y = y / y.sum() * (x.size(0) / (x_max - x_min))
    if not plot:
        return x, y
    plt.plot(x.tolist(), y.tolist(), c=c, lw=lw)
    plt.xlabel(xlab)
    plt.ylabel("Density")


def quantile(samples, probs=(0.25, 0.5, 0.75), dim=-1):
    probs = probs if isinstance(probs, (list, tuple)) else (probs,)
    sorted_samples = samples.sort(dim)[0]
    masses = torch.tensor(probs) * samples.size(dim)
    lower_quantiles = sorted_samples.index_select(dim, masses.long().clamp(min=0))
    upper_quantiles = sorted_samples.index_select(dim, masses.long())
    dim = (samples.dim() + dim) if dim < 0 else dim
    t = masses.frac().reshape((len(probs),) + (-1,) * (samples.dim() - dim - 1))
    quantiles = (1 - t) * lower_quantiles + t * upper_quantiles
    return quantiles


def PI(samples, prob=0.89, dim=-1):
    return quantile(samples, ((1 - prob) / 2., (1 + prob) / 2.), dim)


def HPDI(samples, prob=0.89, dim=-1):
    full_size = samples.size(dim)
    mass = int(prob * full_size)
    sorted_samples = samples.sort(dim)[0]
    intervals = (sorted_samples.index_select(dim, torch.tensor(range(mass, full_size)))
                 - sorted_samples.index_select(dim, torch.tensor(range(full_size - mass))))
    start = intervals.argmin(dim)
    indices = torch.stack([start, start + mass], dim)
    return torch.gather(sorted_samples, dim, indices)


def precis(post, prob=0.89, digits=2):
    mean, std_dev, lower_hpd, upper_hpd = {}, {}, {}, {}
    for latent in post:
        mean[latent] = post[latent].mean().item()
        std_dev[latent] = post[latent].std().item()
        hpdi = HPDI(post[latent], prob)
        lower_hpd[latent] = hpdi[0].item()
        upper_hpd[latent] = hpdi[1].item()
    precis = pd.DataFrame.from_dict({"Mean": mean, "StdDev": std_dev,
                                     "|0.89": lower_hpd, "0.89|": upper_hpd})
    return precis.astype("float").round(digits).loc[post.keys(), :]


def glimmer(family="normal", **kwargs):
    print("def model({}):".format(", ".join(kwargs.keys())))
    print("    intercept = pyro.sample('Intercept', dist.Normal(0, 10))")
    mu_str = "    mu = intercept + "
    args = list(kwargs)
    for i, latent in enumerate(args[:-1]):
        print("    b_{} = pyro.sample('b_{}', dist.Normal(0, 10))".format(latent, latent))
        mu_str += "b_{} * {}".format(latent, latent)
    print(mu_str)
    print("    sigma = pyro.sample('sigma', dist.HalfCauchy(2))")
    print("    with pyro.plate('plate'):")
    print("        return pyro.sample('{}', dist.Normal(mu, sigma), obs={})"
          .format(args[-1], args[-1]))


def compare(*models):
    WAIC_dict, WAIC_vec = {}, {}
    for m in models:
        WAIC = m.WAIC(pointwise=True)
        WAIC_vec[m.name] = WAIC.pop("WAIC_vec")
        WAIC_dict[m.name] = {w_name: w.item() for w_name, w in WAIC.items()}
    df = pd.DataFrame.from_dict(WAIC_dict).T.sort_values(by="WAIC")
    df["dWAIC"] = df["WAIC"] - df["WAIC"][0]
    weight = (-0.5 * torch.tensor(df["dWAIC"].values, dtype=torch.float)).exp()
    df["weight"] = (weight / weight.sum()).tolist()
    dSE = []
    for i in range(df.shape[0]):
        WAIC1 = WAIC_vec[df.index[0]]
        WAIC2 = WAIC_vec[df.index[i]]
        dSE.append((WAIC1.size(0) * (WAIC2 - WAIC1).var()).sqrt().item())
    df["dSE"] = dSE
    df.rename(columns={"se": "SE"}, inplace=True)
    return df[["WAIC", "pWAIC", "dWAIC", "weight", "SE", "dSE"]]


def compare_plot(waic_df):
    plt.plot(waic_df["WAIC"], waic_df.index, "ko", fillstyle="none")
    for i, model in enumerate(waic_df.index):
        se_df = waic_df.loc[model, :]
        plt.plot([se_df["WAIC"] - se_df["SE"], se_df["WAIC"] + se_df["SE"]], [model, model],
                 c="k")
        if i > 0:
            plt.plot([se_df["WAIC"] - se_df["dSE"], se_df["WAIC"] + se_df["dSE"]],
                     [i - 0.5, i - 0.5], c="gray")
    plt.plot(waic_df["WAIC"] - 2 * waic_df["pWAIC"], waic_df.index, "ko")
    plt.plot(waic_df["WAIC"][1:], [i + 0.5 for i in range(waic_df.shape[0] - 1)], "^",
             c="gray", fillstyle="none")
    plt.gca().invert_yaxis()
    plt.axvline(x=waic_df["WAIC"][0], c="k", alpha=0.2)
    plt.grid(axis="y", linestyle="--")
    plt.xlabel("deviance")
    plt.title("WAIC")


def coeftab(*models, PI=False):
    coef_df = pd.concat([m.precis().stack() for m in models], axis=1, sort=False)
    coef_df.columns = [m.name for m in models]
    mean_df = coef_df.unstack().xs("Mean", axis=1, level=1)
    keys = []
    for m in models:
        keys += list(m._latent_shapes)
    keys = list(dict.fromkeys(keys))
    mean_df = mean_df.loc[keys, :]
    mean_df.loc["nobs"] = [m.sim(n=1).size(-1) for m in models]
    if PI:
        lower_PI = coef_df.unstack().xs("5.5%", axis=1, level=1).loc[keys, :]
        upper_PI = coef_df.unstack().xs("94.5%", axis=1, level=1).loc[keys, :]
        return mean_df, lower_PI, upper_PI
    return mean_df


def ensemble(data, *models):
    n = int(1e3)
    shape = (n, list(data.values())[0].size(0))
    WAIC_df = compare(*models)
    weight = torch.tensor(WAIC_df["weight"].values, dtype=torch.float)
    models_dict = {m.name: m for m in models}
    links = [models_dict[m].link(**data, n=n).reshape(n, -1).expand(shape) for m in WAIC_df.index]
    sims = [models_dict[m].sim(**data, n=n).reshape(n, -1).expand(shape) for m in WAIC_df.index]
    index = torch.cat([torch.tensor([0]),
                       (weight.cumsum(0) * n).round().long().clamp(max=n)])
    weighted_link = torch.cat([l[index[i]:index[i + 1]] for i, l in enumerate(links)])
    weighted_sim = torch.cat([s[index[i]:index[i + 1]] for i, s in enumerate(sims)])
    return {"link": weighted_link, "sim": weighted_sim}
