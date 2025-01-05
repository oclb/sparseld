from typing import Any, Callable, Dict, List, Optional, Union, Tuple
from typing import Any, Dict, List, Optional, Union
from multiprocessing import Process, Value, Array, cpu_count
import time
import numpy as np
import polars as pl
from .io import partition_variants
from .multiprocessing import ParallelProcessor, SharedData, WorkerManager
from .precision import PrecisionOperator

class BLUP(ParallelProcessor):
    """Computes the best linear unbiased predictor using LDGMs and GWAS summary statistics."""


    @classmethod
    def prepare_block_data(cls, metadata: pl.DataFrame, **kwargs) -> list[tuple]:
        """Split summary statistics into blocks whose positions match the LDGMs.
        
        Args:
            metadata: DataFrame containing LDGM metadata
            **kwargs: Additional arguments from run(), including:
                annotations: Optional DataFrame containing variant annotations
                
        Returns:
            List of block-specific annotation DataFrames, or None if no annotations
        """
        sumstats = kwargs.get('sumstats')
            
        # Partition annotations into blocks
        sumstats_blocks: list[pl.DataFrame] = partition_variants(metadata, sumstats)

        cumulative_num_variants = np.cumsum(np.array([len(df) for df in sumstats_blocks]))
        cumulative_num_variants = [0] + list(cumulative_num_variants[:-1])

        return list(zip(sumstats_blocks, cumulative_num_variants))
    
    @staticmethod
    def create_shared_memory(metadata: pl.DataFrame, block_data: list[tuple], **kwargs) -> SharedData:
        """Create output array with length number of variants in the summary statistics that 
        migtht match to one of the blocks.
        
        Args:
            metadata: Metadata DataFrame containing block information
            block_data: List of block-specific sumstats DataFrames
            **kwargs: Not used
        """
        total_variants = sum([len(df) for df, _ in block_data])
        return SharedData({
            'beta': total_variants,    # BLUP effect sizes
        })
        

    @classmethod
    def process_block(cls, ldgm: PrecisionOperator, flag: Value, 
                     shared_data: SharedData, block_offset: int,
                     block_data: tuple,
                     worker_params: tuple) -> None:
        """Run BLUP on a single block."""
        sigmasq, sample_size, match_by_position = worker_params
        assert isinstance(sigmasq, float), "sigmasq parameter must be a float"
        assert isinstance(block_data, tuple), "block_data must be a tuple"
        sumstats, variant_offset = block_data
        num_variants = len(sumstats)
        
        # Merge annotations with LDGM variant info and get indices of merged variants
        from .io import merge_snplists
        ldgm, sumstat_indices = merge_snplists(
            ldgm, sumstats,
            match_by_position=match_by_position,
            pos_col='POS',
            ref_allele_col='REF',
            alt_allele_col='ALT',
            add_allelic_cols=['Z'],
        )

        # Keep only first occurrence of each index
        first_index_mask = ldgm.variant_info.select(pl.col('index').is_first_distinct()).to_numpy().flatten()
        ldgm.variant_info = ldgm.variant_info.filter(first_index_mask)
        sumstat_indices = sumstat_indices[first_index_mask]

        # Get Z-scores from the merged variant info
        z = ldgm.variant_info.select('Z').to_numpy()

        # Compute the BLUP for this block
        beta = ldgm @ z
        ldgm.update_matrix(np.full(ldgm.shape[0], sample_size*sigmasq))
        beta = np.sqrt(sample_size) * sigmasq * ldgm.solve(beta)
        ldgm.del_factor()

        # Store results for variants that were successfully merged
        beta_reshaped = np.zeros((num_variants,1))
        # Get indices of variants that were actually merged
        beta_reshaped[sumstat_indices, 0] = beta
        
        # Update the shared memory array
        block_slice = slice(variant_offset, variant_offset + num_variants)
        shared_data['beta', block_slice] = beta_reshaped

    @classmethod
    def supervise(cls, manager: WorkerManager, shared_data: Dict[str, Any], block_data: list, **kwargs) -> pl.DataFrame:
        """Supervise worker processes and collect results.
        
        Args:
            manager: Worker manager
            shared_data: Dictionary of shared memory arrays
            **kwargs: Additional arguments
            
        Returns:
            DataFrame containing simulated summary statistics
        """

        manager.start_workers()
        manager.await_workers()
        return shared_data['beta']

    @classmethod
    def compute_blup(cls,
                ldgm_metadata_path: str,
                sumstats: pl.DataFrame,
                sigmasq: float,
                sample_size: float,
                populations: Optional[Union[str, List[str]]] = None,
                chromosomes: Optional[Union[int, List[int]]] = None,
                num_processes: Optional[int] = None,
                run_in_serial: bool = False,
                match_by_position: bool = False,
                ) -> np.ndarray:
        """Simulate GWAS summary statistics for multiple LD blocks.
        
        Args:
            ldgm_metadata_path: Path to metadata CSV file
            sumstats: Sumstats dataframe containing Z scores
            populations: Optional population name
            chromosomes: Optional chromosome or list of chromosomes
            
        Returns:
            Array of BLUP effect sizes, same length as sumstats
        """ 
        if run_in_serial:
            return cls.run_serial(
            ldgm_metadata_path=ldgm_metadata_path,
            populations=populations,
            chromosomes=chromosomes,
            worker_params=(sigmasq,sample_size,match_by_position),
            sumstats=sumstats)

        return cls.run(
            ldgm_metadata_path=ldgm_metadata_path,
            populations=populations,
            chromosomes=chromosomes,
            worker_params=(sigmasq,sample_size,match_by_position), 
            num_processes=num_processes,
            sumstats=sumstats
        )