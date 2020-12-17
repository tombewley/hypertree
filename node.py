from .utils import *
import numpy as np
import bisect
from sklearn.decomposition import PCA

class Node:
    """
    Class for a node, which is characterised by its samples (sorted_indices of data from source), 
    mean, covariance matrix and minimal and maximal bounding boxes. 
    """
    def __init__(self, source, sorted_indices=None, bb_min=None, bb_max=None, meta={}):
        self.source = source # To refer back to the source class.        
        self.bb_max = np.array(bb_max if bb_max else # If a maximal bounding box has been provided, use that.
                      [[-np.inf, np.inf] for _ in self.source.dim_names]) # Otherwise, bb_max is infinite.
        # These attributes are defined if and when the node is split.
        self.split_dim, self.split_threshold, self.left, self.right, self.gains = None, None, None, None, {} 
        # This dictionary can be used to store miscellaneous meta information about this node.
        self.meta = meta
        # Populate with samples if provided.
        self.populate(sorted_indices)
        # Overwrite minimal bounding box if provided.
        if bb_min: self.bb_min = np.array(bb_min)

    # Dunder/magic methods.
    def __repr__(self): return f"Node with {self.num_samples} samples"
    def __call__(self, *args, **kwargs): return self.membership(*args, **kwargs)
    def __getattr__(self, key): return self.__getitem__(key)
    def __getitem__(self, key): 
        if type(key) == tuple: 
            try: return self.stat(key) # For statistical attributes.
            except: pass
        return self.meta[key]
    def __setitem__(self, key, val): self.meta[key] = val
    def __contains__(self, idx): return idx in self.sorted_indices[:,0] 

    def populate(self, sorted_indices):
        """
        Populate the node with samples and compute statistics.
        """
        if sorted_indices is None: sorted_indices = np.empty((0, len(self.source.dim_names)))
        self.sorted_indices = sorted_indices
        self.num_samples, num_dims = sorted_indices.shape
        if self.num_samples > 0: 
            X = self.source.data[sorted_indices[:,0]] # Won't actually store this; order doesn't matter.
            self.mean = np.mean(X, axis=0)
            # Minimal bounding box is defined by the samples.
            self.bb_min = np.array([np.min(X, axis=0), np.max(X, axis=0)]).T
            if self.num_samples > 1:
                self.cov = np.cov(X, rowvar=False, ddof=0) # ddof=0 overrides bias correction.                
        else: 
            self.mean = np.full(num_dims, np.nan)
            self.bb_min = np.full((num_dims, 2), np.nan)
        try: self.cov
        except: self.cov = np.zeros((num_dims, num_dims))
        self.cov_sum = self.cov * self.num_samples
        self.var_sum = np.diag(self.cov_sum)

    def membership(self, x, mode, contain=False):
        """
        Evaluate the membership of x in this node. Each element of x can be None (ignore),
        a scalar (treat as in regular prediction), or a (min, max) interval.
        """
        per_dim = []
        for xd, lims_min, lims_max, mean in zip(x, self.bb_min, self.bb_max, self.mean):
            try:
                # For marginalised (None <=> (-inf, inf) interval).
                if xd is None or np.isnan(xd): 
                    if mode == "fuzzy": per_dim.append(1) 
                # For scalar.
                elif mode == "mean": # Equal to mean.
                    if not xd == mean: return 0 
                elif mode in ("min", "max"): # Inside bounding box.
                    lims = (lims_min if mode == "min" else lims_max)
                    if not(xd >= lims[0] and xd <= lims[1]): return 0
                elif mode == "fuzzy": # Fuzzy membership using both bounding boxes.
                    to_max_l = xd - lims_max[0]
                    to_max_u = xd - lims_max[1]
                    # Outside bb_max.
                    if not(to_max_l >= 0 and to_max_u <= 0): return 0 
                    else:
                        to_min_l = xd - lims_min[0]
                        above_min_l = (to_min_l >= 0)
                        to_min_u = xd - lims_min[1]
                        below_min_u = (to_min_u <= 0)
                        # Inside bb_min.
                        if (above_min_l and below_min_u): per_dim.append(1)
                        # Otherwise (partial membership).
                        else: 
                            # Below lower of bb_min.
                            if not(above_min_l): per_dim.append(to_max_l / (to_max_l - to_min_l))
                            # Above upper of bb_min.
                            else: per_dim.append(to_max_u / (to_max_u - to_min_u))
                else: raise ValueError()
            except:
                # For (min, max) interval.
                if mode == "fuzzy": raise NotImplementedError("Cannot handle intervals in fuzzy mode.")
                elif mode == "mean": # Contains mean.
                    if not (xd[0] <= mean <= xd[1]): return 0 
                elif mode in ("min", "max"): # Intersected/contained by bounding box.
                    lims = (lims_min if mode == "min" else lims_max)
                    compare = [[i >= l for i in xd] for l in lims]
                    if contain:
                        if (not(compare[0][0]) or compare[1][1]): return 0
                    elif (not(compare[0][1]) or compare[1][0]): return 0
        if mode == "fuzzy":
            return abs(min(per_dim)) # Compute total membership using the minimum T-norm.
        return 1

    def stat(self, attr):
        """
        Return a statistical attribute for this node.
        """
        dim = self.source.idxify(attr[1])
        if len(attr) == 3: dim2 = self.source.idxify(attr[2])
        # Mean, standard deviation, or sqrt of covarance (std_c).
        if attr[0] == 'mean': return self.mean[dim]
        if attr[0] == 'std': return np.sqrt(self.cov[dim,dim])
        if attr[0] == 'std_c': return np.sqrt(self.cov[dim,dim2])
        if attr[0] in ('median','iqr','q1q3'):
            # Median, interquartile range, or lower and upper quartiles.
            q1, q2, q3 = np.quantile(self.source.data[self.sorted_indices[:,dim],dim], (.25,.5,.75))
            if attr[0] == 'median': return q2
            if attr[0] == 'iqr': return q3-q1
            if attr[0] == 'q1q3': return (q1,q3)
        raise ValueError()
    
    def pca(self, dims=None, n_components=None, whiten_by="local"):
        """
        Perform principal component analysis on the data at this node, whitening beforehand
        to ensure that large dimensions do not dominate.
        """
        X = self.source.data[self.sorted_indices[:,0][:,None],dims]
        if X.shape[0] <= 1: return None, None
        if dims is None: dims = np.arange(len(self.source.dim_names))
        else: dims = source.idxify(dims)
        # Whiten data, using either local or global standard deviation.
        mean = X.mean(axis=0)
        std = X.std(axis=0) if whiten_by == 'local' else (1 / (self.source.global_var_scale[dims] ** 0.5))   
        X = (X - mean) / std
        # Perform PCA on whitened data.
        pca = PCA(n_components=n_components); pca.fit(X)
        # Return components scaled back by standard deviation, and explained variance ratio.
        return (pca.components_ * std), pca.explained_variance_ratio_

    def _do_manual_split(self, split_dim, split_threshold):
        """
        Split using a manually-defined split_dim and split_threshold.
        """
        if not(self.bb_max[split_dim][0] <= split_threshold <= self.bb_max[split_dim][1]): return False
        self.split_dim, self.split_threshold = split_dim, split_threshold
        # Split samples.
        data = self.source.data[self.sorted_indices[:,self.split_dim], self.split_dim]
        split_index = bisect.bisect(data, self.split_threshold)
        left, right = split_sorted_indices(self.sorted_indices, self.split_dim, split_index)
        # Split bounding box.
        bb_max_left = self.bb_max.copy(); bb_max_left[self.split_dim,1] = self.split_threshold
        bb_max_right = self.bb_max.copy(); bb_max_right[self.split_dim,0] = self.split_threshold
        # Make children.
        self.left = Node(self.source, sorted_indices=left, bb_max=bb_max_left)
        self.right = Node(self.source, sorted_indices=right, bb_max=bb_max_right)
        return True

    def _do_greedy_split(self, split_dims, eval_dims, corr=False, one_sided=False, pop_power=.5):
        """
        Find and implement the greediest split given split_dims and eval_dims.
        """
        splits, extra = self._find_greedy_splits(split_dims, eval_dims, corr, one_sided, pop_power)
        if splits:
            # Sort splits by quality and choose the single best.
            split_dim, split_point, qual, index, (left, right) = sorted(splits, key=lambda x: x[2], reverse=True)[0]        
            if qual > 0:
                self.split_dim = split_dim
                # Compute numerical threshold to split at: midpoint of samples either side.
                self.split_threshold = (self.source.data[left[-1,split_dim],split_dim] + self.source.data[right[0,split_dim],split_dim]) / 2
                if one_sided: # Only create the child for which the split is made.
                    self.eval_child_and_dims = index
                    do_right = bool(self.eval_child_and_dims[0])
                    print(f'Split @ {self.split_dim}={self.split_threshold} for child {self.eval_child_and_dims[0]} cov({self.source.dim_names[eval_dims[self.eval_child_and_dims[1]]]},{self.source.dim_names[eval_dims[self.eval_child_and_dims[2]]]})')
                else: self.gains["immediate"] = extra
                # Split bounding box and make children.
                if (not one_sided) or (not do_right):
                    bb_max_left = self.bb_max.copy(); bb_max_left[self.split_dim,1] = self.split_threshold
                    self.left = Node(self.source, sorted_indices=left, bb_max=bb_max_left)
                if (not one_sided) or do_right:
                    bb_max_right = self.bb_max.copy(); bb_max_right[self.split_dim,0] = self.split_threshold
                    self.right = Node(self.source, sorted_indices=right, bb_max=bb_max_right)
                return True, extra
        return False, extra

    def _find_greedy_splits(self, split_dims, eval_dims, corr=False, one_sided=False, pop_power=.5):
        """
        Try splitting the node along several split_dims, measuring quality using eval_dims.  
        Return the best split from each dim.
        """
        if corr:
            # Sequences of num_samples for left and right children.
            n = np.arange(self.num_samples)
            n = np.vstack((n, np.flip(n+1)))[:,:,None,None]
        splits, extra = [], []
        for split_dim in split_dims:
            # Cannot split on a dim if there is no variance, so skip.
            if self.var_sum[split_dim] == 0: continue
            # Evaluate splits along this dim, returning (co)variance sums.
            cov_or_var_sum = self._eval_splits_one_dim(split_dim, eval_dims, cov=corr)
            if corr: 
                # TODO: Fully vectorise this.
                r2 = np.array([np.array([cov_to_r2(cov_c_n) # Convert cov to R^2...
                               for cov_c_n in cov_c]) for cov_c in # ...for each child and each num_samples...
                               cov_or_var_sum / n]) # ...where cov is computed by dividing cov_sum by num_samples.          
                # Scaling incentivises large populations. 
                r2_scaled = r2 * (np.log2(n-1) ** pop_power)
                # r2_scaled = r2 * n / self.num_samples     
                if one_sided:           
                    # Split quality = maximum value of (R^2 * log2(population-1)**pop_power).
                    right, split_point, d1, d2 = np.unravel_index(np.nanargmax(r2_scaled), r2_scaled.shape)
                    qual_max = r2_scaled[(right, split_point, d1, d2)] - r2_scaled[(1, 0, d1, d2)]
                    extra.append(r2_scaled) # Extra = r2_scaled at all points.
                else:
                    pca = [[np.linalg.eig(cov_c_n) if not np.isnan(cov_c_n).any() else None
                           for cov_c_n in cov_c] for cov_c in 
                           cov_or_var_sum / n]
                    return pca
            else:    
                if one_sided: 
                    raise NotImplementedError()
                else:
                    # Split quality = sum of reduction in dimensions-scaled variance sums.
                    gain_per_dim = (cov_or_var_sum[1,0] - cov_or_var_sum.sum(axis=0))
                    qual = (gain_per_dim * self.source.global_var_scale[eval_dims]).sum(axis=1)
                    split_point = np.argmax(qual) # Greedy split is the one with the highest quality.                    
                    qual_max = qual[split_point]
                    extra.append(gain_per_dim[split_point]) # Extra = gain_per_dim at split point.
            # Store split info.
            splits.append((split_dim, split_point, qual_max, (right, d1, d2) if corr else None,
                           split_sorted_indices(self.sorted_indices, split_dim, split_point)))
        return splits, np.array(extra)

    def _eval_splits_one_dim(self, split_dim, eval_dims, cov=False):
        """
        Try splitting the node along one split_dim, calculating (co)variance sums along eval_dims.  
        """
        eval_data = self.source.data[self.sorted_indices[:,split_dim][:,None],eval_dims] 
        d = len(eval_dims)
        mean = np.zeros((2,self.num_samples,d))
        if cov: # For full covariance matrix.
            cov_sum = np.zeros((2,self.num_samples,d,d))
            cov_sum[1,0] = self.cov_sum[eval_dims[:,None],eval_dims]
        else: # Just variances (diagonal of cov).
            var_sum = mean.copy()
            var_sum[1,0] = self.var_sum[eval_dims]
        mean[1,0] = self.mean[eval_dims] 
        for num_left in range(1,self.num_samples): 
            num_right = self.num_samples - num_left
            x = eval_data[num_left-1]
            if cov:
                mean[0,num_left], cov_sum[0,num_left] = increment_mean_and_cov_sum(num_left,  mean[0,num_left-1], cov_sum[0,num_left-1], x, 1)
                mean[1,num_left], cov_sum[1,num_left] = increment_mean_and_cov_sum(num_right, mean[1,num_left-1], cov_sum[1,num_left-1], x, -1)
            else:
                mean[0,num_left], var_sum[0,num_left] = increment_mean_and_var_sum(num_left,  mean[0,num_left-1], var_sum[0,num_left-1], x, 1)
                mean[1,num_left], var_sum[1,num_left] = increment_mean_and_var_sum(num_right, mean[1,num_left-1], var_sum[1,num_left-1], x, -1)            
        return cov_sum if cov else var_sum