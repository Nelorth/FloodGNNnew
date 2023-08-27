import pandas as pd
import tarfile
import torch
import urllib.request

from torch_geometric.data import Data, Dataset
from tqdm import tqdm


class LamaHDataset(Dataset):
    DATA_URL = "https://zenodo.org/record/5153305/files/1_LamaH-CE_daily_hourly.tar.gz"

    def __init__(self, root, years=range(2000, 2018), window_size=24, stride_length=1, lead_time=1, normalized=False):
        super().__init__(root)  # calls download() and process() if necessary

        self.window_size_hrs = window_size
        self.stride_length_hrs = stride_length
        self.lead_time_hrs = lead_time
        self.normalized = normalized
        self.years = years
        self.year_sizes = [(24 * (365 + int(year % 4 == 0)) - (window_size + lead_time)) // stride_length + 1
                           for year in years]

        adjacency = pd.read_csv(self.processed_paths[0])
        self.gauges = list(sorted(set(adjacency["ID"]).union(adjacency["NEXTDOWNID"])))
        rev_index = {gauge_id: i for i, gauge_id in enumerate(self.gauges)}
        edge_cols = adjacency[["ID", "NEXTDOWNID"]].applymap(lambda x: rev_index[x])
        self.edge_index = torch.tensor(edge_cols.values.transpose(), dtype=torch.long)
        weight_cols = adjacency[["dist_hdn", "elev_diff", "strm_slope"]]
        self.edge_attr = torch.tensor(weight_cols.values, dtype=torch.float)

        statistics = pd.read_csv(self.processed_paths[1], index_col="ID")
        self.mean = torch.tensor(statistics["mean"].values, dtype=torch.float).unsqueeze(-1)
        self.std = torch.tensor(statistics["std"].values, dtype=torch.float).unsqueeze(-1)

        self.year_tensors = [[] for _ in years]
        print("Loading dataset into memory...")
        for gauge_id in tqdm(self.gauges):
            df = pd.read_csv(f"{self.raw_dir}/{self.raw_file_names[1]}/hourly/ID_{gauge_id}.csv",
                             sep=";", usecols=["YYYY", "qobs"])
            if normalized:
                df["qobs"] = (df["qobs"] - statistics.loc[gauge_id, "mean"]) / statistics.loc[gauge_id, "std"]
            for i, year in enumerate(years):
                self.year_tensors[i].append(torch.tensor(df[df["YYYY"] == year]["qobs"].values, dtype=torch.float))
        self.year_tensors[:] = map(torch.stack, self.year_tensors)

    @property
    def raw_file_names(self):
        return ["B_basins_intermediate_all/1_attributes", "D_gauges/2_timeseries"]

    @property
    def processed_file_names(self):
        return ["adjacency.csv", "statistics.csv"]

    def download(self):
        print("Downloading LamaH-CE from Zenodo to", self.raw_dir)
        total_size = int(urllib.request.urlopen(self.DATA_URL).info().get('Content-Length'))
        with tqdm(total=total_size, unit="B", unit_scale=True, unit_divisor=1024) as pbar:
            filename, _ = urllib.request.urlretrieve(self.DATA_URL, reporthook=lambda _, n, __: pbar.update(n))
        with tarfile.open(filename) as archive:
            archive.extractall(self.raw_dir, members=list(
                filter(lambda t: t.name.startswith(tuple(self.raw_file_names)), archive.getmembers())))

    def process(self):
        stream_dist = pd.read_csv(f"{self.raw_dir}/{self.raw_file_names[0]}/Stream_dist.csv", sep=";")
        stream_dist.drop(columns="strm_slope", inplace=True)  # will re-calculate from dist_hdn and elev_diff

        def collect_upstream(id):
            predecessors = set(stream_dist[stream_dist["NEXTDOWNID"] == id]["ID"])
            if len(predecessors) == 0:
                return {id}
            else:
                return {id}.union(*(collect_upstream(pred) for pred in predecessors))

        connected_gauges = set(stream_dist["ID"]).union(stream_dist["NEXTDOWNID"])
        danube_gauges = set(collect_upstream(399))  # 399 is most downstream Danube gauge
        assert danube_gauges.issubset(connected_gauges)

        feasible_gauges = set()
        qobs_statistics = pd.DataFrame(columns=["mean", "std", "min", "median", "max"], index=pd.Index([], name="ID"))
        for gauge_id in tqdm(danube_gauges, desc="Gauge filtering"):
            ts = pd.read_csv(f"{self.raw_dir}/{self.raw_file_names[1]}/hourly/ID_{gauge_id}.csv",
                             sep=";", usecols=["YYYY", "MM", "DD", "hh", "mm", "qobs"])
            if (ts["qobs"] >= 0).all():
                sub_ts = ts[(ts["YYYY"] >= 2000) & (ts["YYYY"] <= 2017)]
                if len(sub_ts) == (18 * 365 + 5) * 24:  # number of hours in 2000-2017
                    feasible_gauges.add(gauge_id)
                    qobs_statistics.loc[gauge_id] = [ts["qobs"].mean(), ts["qobs"].std(),
                                                     ts["qobs"].min(), ts["qobs"].median(), ts["qobs"].max()]
                    assert tuple(sub_ts.iloc[0, :5]) == (2000, 1, 1, 0, 0) \
                           and tuple(sub_ts.iloc[-1, :5]) == (2017, 12, 31, 23, 0)
        print("Determined", len(feasible_gauges), "feasible gauges.")
        assert 399 in feasible_gauges

        for gauge_id in tqdm(connected_gauges - feasible_gauges, desc="Bad gauge removal"):
            incoming_edges = stream_dist.loc[stream_dist["NEXTDOWNID"] == gauge_id]
            outgoing_edges = stream_dist.loc[stream_dist["ID"] == gauge_id]

            stream_dist.drop(labels=incoming_edges.index, inplace=True)
            stream_dist.drop(labels=outgoing_edges.index, inplace=True)

            bypass = incoming_edges.merge(outgoing_edges, how="cross", suffixes=["", "_"])
            bypass["NEXTDOWNID"] = bypass["NEXTDOWNID_"]
            bypass["dist_hdn"] += bypass["dist_hdn_"]
            bypass["elev_diff"] += bypass["elev_diff_"]
            stream_dist = pd.concat([stream_dist, bypass[["ID", "NEXTDOWNID", "dist_hdn", "elev_diff"]]],
                                    ignore_index=True, copy=False)

            stream_dist.reset_index()

        print("Saving final adjacency list to", self.processed_paths[0])
        stream_dist["strm_slope"] = stream_dist["elev_diff"] / stream_dist["dist_hdn"]
        stream_dist.sort_values(by="ID", inplace=True)
        stream_dist.to_csv(self.processed_paths[0], index=False)

        print("Saving discharge summary statistics to", self.processed_paths[1], end="\n\n")
        qobs_statistics.to_csv(self.processed_paths[1], index=True)

    def len(self):
        return sum(self.year_sizes)

    def get(self, idx):
        year_tensor, offset = self._decode_index(idx)
        x = year_tensor[:, offset:(offset + self.window_size_hrs)]
        y = year_tensor[:, offset + self.window_size_hrs + (self.lead_time_hrs - 1)]
        return Data(x=x, y=y.unsqueeze(-1), edge_index=self.edge_index, edge_attr=self.edge_attr)

    def normalize(self, x):
        return (x - self.mean) / self.std

    def denormalize(self, x):
        return self.std * x + self.mean

    def _decode_index(self, idx):
        for i, size in enumerate(self.year_sizes):
            idx -= size
            if idx < 0:
                return self.year_tensors[i], self.stride_length_hrs * (idx + size)
        raise AssertionError("This cannot happen!")
