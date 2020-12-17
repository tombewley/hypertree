from .model import Model
from .tree import Tree
from .node import Node
from .utils import *
import numpy as np
from tqdm import tqdm

class Source:
    """
    Master class for centrally storing data and building models.
    """
    def __init__(self, data, dim_names):
        self.data = data
        assert len(dim_names) == data.shape[1]
        self.dim_names = dim_names
        if data.shape[0]:
            # Sort data along each dimension up front.
            self.all_sorted_indices = np.argsort(data, axis=0) 
            # Scale factors for variance are reciprocals of global variance.
            var = np.var(data, axis=0)
            var[var==0] = 1 # Prevent div/0 error.
            self.global_var_scale = 1 / var
        # Empty dictionary for storing models.
        self.models = {}

    # Dunder/magic methods.
    def __repr__(self): return f"Source with {len(self.data)} samples and {len(self.models)} models"
    def __getitem__(self, name): return self.models[name]

    def subset(self, bb=None, subsample=None):
        """
        Retrieve a subset of the data by per-dimension filtering and/or random subsampling.
        """
        sorted_indices = self.all_sorted_indices
        if bb is not None: sorted_indices = bb_filter_sorted_indices(self, sorted_indices, bb)
        return subsample_sorted_indices(sorted_indices, subsample)

    def tree_depth_first(self, name, split_dims, eval_dims, sorted_indices=None, max_depth=np.inf, 
                         corr=False, one_sided=False, pop_power=.5):
        """
        Grow a tree depth-first to max_depth using samples specified by sorted_indices. 
        """
        if corr: assert len(eval_dims) > 1
        split_dims, eval_dims, sorted_indices = self._preflight_check(split_dims, eval_dims, sorted_indices)
        def _recurse(node, depth):
            if node is None: return # This will be the case 50% of the time if doing one-sided.
            if depth < max_depth:
                ok, _ = node._do_greedy_split(split_dims, eval_dims, corr, one_sided, pop_power)
                if ok: _recurse(node.left, depth+1); _recurse(node.right, depth+1)
        root = Node(self, sorted_indices=sorted_indices) 
        _recurse(root, 0)
        self.models[name] = Tree(name, root, split_dims, eval_dims)
        return self.models[name]

    def tree_best_first(self, name, split_dims, eval_dims, sorted_indices=None, max_num_leaves=np.inf): 
        """
        Grow a tree best-first to max_num_leaves using samples specified by sorted_indices. 
        """
        split_dims, eval_dims, sorted_indices = self._preflight_check(split_dims, eval_dims, sorted_indices)
        with tqdm(total=max_num_leaves) as pbar:
            root = Node(self, sorted_indices=sorted_indices) 
            priority = np.dot(root.var_sum[eval_dims], self.global_var_scale[eval_dims])
            queue = [(root, priority)]
            pbar.update(1); num_leaves = 1
            while num_leaves < max_num_leaves and len(queue) > 0:
                queue.sort(key=lambda x: x[1], reverse=True)
                # Try to split the highest-priority leaf.
                node, _ = queue.pop(0) 
                ok, _ = node._do_greedy_split(split_dims, eval_dims)
                if ok:    
                    pbar.update(1); num_leaves += 1
                    # If split made, add the two new leaves to the queue.
                    queue += [(node.left,
                               np.dot(node.left.var_sum[eval_dims], self.global_var_scale[eval_dims])),
                              (node.right,
                               np.dot(node.right.var_sum[eval_dims], self.global_var_scale[eval_dims]))]
        self.models[name] = Tree(name, root, split_dims, eval_dims)
        return self.models[name]

    def model_from_dict(self, name, d):
        """
        Create a flat model from a dictionary object.
        """
        leaves = []
        for node in (d.values() if type(d) == dict else d):
            # Get the maximal bounding box in the correct form. 
            bb_max = self.listify(node["bb_max"], placeholder=[-np.inf,np.inf], duplicate_singletons=True)
            # Also add minimal bounding box if specified.
            if "bb_min" in node:
                bb_min = self.listify(node["bb_min"], placeholder=[-np.inf,np.inf], duplicate_singletons=True)    
            # Add a new leaf.
            leaves.append(Node(self, bb_min=bb_min, bb_max=bb_max, meta=node["meta"]))
        self.models[name] = Model(name, leaves)
        return self.models[name]
    
    def tree_from_dict(self, name, d): 
        """
        Create a tree from a dictionary object.
        """
        def _recurse(node, n): 
            if n in d:
                if not node._do_manual_split(d[n]["split_dim"], d[n]["split_threshold"]):
                    raise ValueError(f"Invalid split threshold for node {n}: \"{d[n]}\".")
                _recurse(node.left, d[n]["left"])
                _recurse(node.right, d[n]["right"])
        root = Node(self, sorted_indices=self.all_sorted_indices)
        _recurse(root, 1) # Root node must have key of 1 in dict.
        split_dims, eval_dims = list(set(v["split_dim"] for v in d.values())), [] # NOTE: No eval dims.
        self.models[name] = Tree(name, root, split_dims, eval_dims)
        return self.models[name]

    def tree_from_func(self, name, func):
        """
        Create a tree from a well-formed nested if-then function in Python.
        Tests must use the < operator; split_dims can either be identified with indices, e.g. x[0],
        or with a valid entry in self.dim_names.
        """
        from dill.source import getsource
        lines = [l.strip() for l in getsource(func).split("\n")[:-1]]
        assert lines[0][:3] == "def"
        def _recurse(node, n):
            if lines[n][0] == "#": return _recurse(node, n + 1) 
            elif lines[n][:2] == "if":
                d, o, t = lines[n][3:-1].split(" ")
                assert o in ("<", ">=")
                try: split_dim = int(d.split("[")[1][:-1]) # If index specified.
                except: split_dim = self.dim_names.index(d) # If dim_name specified.
                split_dims.add(split_dim)
                if not node._do_manual_split(split_dim, float(t)):
                    raise ValueError(f"Invalid split threshold at line {n}: \"{lines[n]}\".")
                n = _recurse(node.left if o == "<" else node.right, n + 1)
                assert lines[n] == "else:"
                n = _recurse(node.right if o == "<" else node.left, n + 1)
            elif lines[n][:6] == "return": n += 1 # NOTE: Not doing anything with returns.
            else: raise ValueError(f"Parse error at line {n}: \"{lines[n]}\".")
            return n
        split_dims, eval_dims = set(), [] # NOTE: No eval dims.
        root = Node(self, sorted_indices=self.all_sorted_indices)
        _recurse(root, 1)
        self.models[name] = Tree(name, root, sorted(split_dims), eval_dims)
        return self.models[name]

    def idxify(self, *args):
        """
        Dims are convenient to specify as names. 
        This method converts them into numerical indices.
        """
        dims_idx = [] 
        for dims in args:
            if type(dims) == list:
                dims = [self.dim_names.index(d) if type(d) != int else d for d in dims]
            elif type(dims) == str: dims = self.dim_names.index(dims) 
            dims_idx.append(dims)
        return dims_idx if len(dims_idx) > 1 else dims_idx[0]

    def listify(self, x, placeholder=None, duplicate_singletons=False):
        """
        Hyperrectangular sets are convenient to specify as dictionaries.
        This method converts them into lists.
        """
        if type(x) != dict: return x # If not a dict, return unchanged.
        dim_list = [placeholder for _ in self.dim_names]  
        for dim, value in dim_dict.items():
            if duplicate_singletons:
                try: len(value)
                except: value = [value, value]
            dim_list[dim_names.index(dim)] = value
        return dim_list

    def _preflight_check(self, split_dims, eval_dims, sorted_indices):
        split_dims, eval_dims = self.idxify(split_dims, eval_dims)
        # If indices not specified, use all.
        if sorted_indices is None: sorted_indices = self.all_sorted_indices
        return np.array(split_dims), np.array(eval_dims), sorted_indices