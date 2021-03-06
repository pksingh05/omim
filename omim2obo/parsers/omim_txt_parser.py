"""Text parsing utilities"""
import json
import logging
import os
import sys
from collections import defaultdict
from pathlib import PosixPath
from typing import List, Dict, Tuple, Union

import requests
import re
import pandas as pd
# from rdflib import URIRef

from omim2obo.config import config, DATA_DIR
from omim2obo.omim_type import OmimType
# from omim2obo.omim_client import OmimClient
# from omim2obo.omim_code_scraper.omim_code_scraper import get_codes_by_yyyy_mm


LOG = logging.getLogger('omim2obo.parser.omim_titles_parser')


def retrieve_mim_file(file_name: str, download=False, return_df=False) -> Union[List[str], pd.DataFrame]:
    """
    Retrieve OMIM downloadable text file from the OMIM download server
    :param return_df: If False, returns List[str] of each line in the file, else a DataFrame.
    """
    file_headers = {
        'mim2gene.txt': '# MIM Number	MIM Entry Type (see FAQ 1.3 at https://omim.org/help/faq)	Entrez Gene ID (NCBI)	Approved Gene Symbol (HGNC)	Ensembl Gene ID (Ensembl)',
        'genemap2.txt': '# Chromosome	Genomic Position Start	Genomic Position End	Cyto Location	Computed Cyto Location	MIM Number	Gene Symbols	Gene Name	Approved Gene Symbol	Entrez Gene ID	Ensembl Gene ID	Comments	Phenotypes	Mouse Gene Symbol/ID'
    }
    mim_file: PosixPath = DATA_DIR / file_name
    mim_file_tsv: str = str(mim_file).replace('.txt', '.tsv')

    if download:
        url = f'https://data.omim.org/downloads/{config["API_KEY"]}/{file_name}'
        # todo: This doesn't work for genemap2.txt. But does the previous URL work? If so, why not just use that?
        if file_name == 'mim2gene.txt':
            url = f'https://omim.org/static/omim/data/{file_name}'
        resp = requests.get(url)
        if resp.status_code == 200:
            text = resp.text
            if not text.startswith('<!DOCTYPE html>'):
                # Save file
                with open(mim_file, 'w') as fout:
                    fout.write(text)
                # TODO: mim2gene.txt & genemap2.txt: Need to uncomment out the first line
                #   modify 'text'. what's the nature of it? how to edit just that one line?

                # todo: This is brittle in that it depends on these headers not changing. Would be better, eventually,
                #  to read the lines into a list, then find the first line w/out a comment, get its index, then -1 to
                #  get index of prev line, then use that to remove the leading '# ' from that line.
                # todo: also would be good to do this, because the other TSVs won't have their headers. It doesn't
                #  matter that much atm, because these files aren't used for anything programmatic.
                if file_name in file_headers:
                    header = file_headers[file_name]
                    text = text.replace(header, header[2:])  # removes leading comment
                with open(mim_file_tsv, 'w') as fout:
                    fout.write(text)
                LOG.info(f'{file_name} retrieved and updated')
            else:
                raise RuntimeError('Unexpected response: ' + text)
        else:
            msg = 'Response from server: ' + resp.text
            # LOG.warning(msg)
            # with open(mim_file, 'r') as fin:
            #     lines = fin.readlines()
            # LOG.warning('Failed to retrieve mimTitles.txt. Using the cached file.')
            raise RuntimeError(msg)

    if return_df:
        df = pd.read_csv(mim_file_tsv, comment='#', sep='\t')
        return df
    else:
        with open(mim_file, 'r') as fin:
            lines: List[str] = fin.readlines()
            return lines


def parse_mim_genes(lines):
    mim_genes = {}
    for line in lines:
        if line.startswith('#'):
            continue
        tokens = line.split('\t')
        if not tokens or tokens == ['']:
            continue
        if tokens[1] in ['moved/removed', 'phenotype', 'predominantly phenotypes']:
            continue
        if len(tokens) == 5:
            mim_number, entry_type, entrez_id, gene_symbol, ensembl_id = tokens
            mim_genes[mim_number] = (entry_type, entrez_id, gene_symbol, ensembl_id)
        else:
            LOG.warning("mim2gene - invalid line: ", line)
    return mim_genes


def parse_omim_id(omim_id, log_success_case_warnings=False):
    """
    Tries to fix an OMIM_ID
    :param omim_id:
    :return: If omim_id is in the correct format, return the id. Otherwise, return the fixed id.
    """
    if omim_id.isdigit() and len(omim_id) == 6:
        return omim_id
    else:
        if log_success_case_warnings:
            LOG.warning(f'Trying to repair malformed omim id: {omim_id}')
        m = re.match(r'\{(\d{6})\}', omim_id)
        if m:
            if log_success_case_warnings:
                LOG.warning(f'Repaired malformed omim id: {m.group(1)}')
            return m.group(1)

        m = re.match(r'(\d{6}),', omim_id)
        if m:
            if log_success_case_warnings:
                LOG.warning(f'Repaired malformed omim id: {m.group(1)}')
            return m.group(1)

        LOG.warning(f'Failed to repair malformed omim id: {omim_id}')
        return None


def parse_mim_titles(lines):
    """
    Parse the omim titles
    :param lines:
    :return: omim_type and omim_replaced, dicts that captures the type of the omim_id and if they've been replaced
    """
    omim_type = {}
    omim_replaced = {}
    declared_to_type = {
        'Caret': OmimType.OBSOLETE,  # 'HP:0031859',  # obsolete
        'Asterisk': OmimType.GENE,  # 'SO:0000704',  # gene
        'NULL': OmimType.SUSPECTED,  # 'NCIT:C71458',  # Suspected
        'Number Sign': OmimType.PHENOTYPE,  # 'UPHENO:0001001',  # phenotype
        'Percent': OmimType.HERITABLE_PHENOTYPIC_MARKER,  # 'SO:0001500',  # heritable_phenotypic_marker
        'Plus': OmimType.HAS_AFFECTED_FEATURE,  # 'GENO:0000418',  # has_affected_feature
    }
    for line in lines:
        if len(line) == 0 or line.isspace() or line[0] == '#':
            continue  # skip the comment lines
        declared, omim_id, pref_label, alt_label, inc_label = [i.strip() for i in line.split('\t')]
        if not declared and not omim_id and not pref_label and not alt_label and not inc_label:
            continue
        if declared in declared_to_type:
            omim_type[omim_id] = (declared_to_type[declared], pref_label, alt_label, inc_label)
        else:
            LOG.error('Unknown OMIM type line %s', line)
        if declared == 'Caret':  # moved|removed|split -> moved twice
            omim_replaced[omim_id] = []
            if pref_label.startswith('MOVED TO '):
                replaced = [parse_omim_id(rep) for rep in pref_label[9:].split() if rep != 'AND']
                omim_replaced[omim_id] = list(filter(None, replaced))
    return omim_type, omim_replaced


def parse_phenotypic_series_titles(lines) -> Dict[str, List]:
    ret = defaultdict(list)
    for line in lines:
        if line.startswith('#'):
            continue
        tokens = line.split('\t')
        if not tokens or tokens == ['']:
            continue
        ps_id = tokens[0].strip()[2:]
        if len(tokens) == 2:
            ret[ps_id].append(tokens[1].strip())
            ret[ps_id].append([])
        if len(tokens) == 3:
            ret[ps_id][1].append(tokens[1])
    return ret


def parse_gene_map(lines):
    """To be implemented"""
    print(lines)
    ...


def get_hgnc_map(filename, symbol_col, mim_col='MIM Number') -> Dict:
    """Get HGNC Map"""
    map = {}
    input_path = os.path.join(DATA_DIR, filename)
    df = pd.read_csv(input_path, delimiter='\t', comment='#').fillna('')
    df[mim_col] = df[mim_col].astype(int)  # these were being read as floats

    for index, row in df.iterrows():
        symbol = row[symbol_col]
        if symbol:
            # Useful to read as `int` to catch any erroneous entries, but convert to str for compatibility with rest of
            # codebase, which is currently reading as `str` for now.
            map[str(row[mim_col])] = symbol

    return map


def parse_mim2gene(lines, filename='mim2gene.tsv', filename2='genemap2.tsv') -> Tuple[Dict, Dict, Dict]:
    """Parse OMIM # 2 gene file
    todo: ideally replace this whole thing with pandas
    todo: How to reconcile inconsistent mim#::hgnc_symbol mappings?
    todo: Generate inconsistent mapping report as csv output instead and print a single warning with path to that file.
    """
    # Gene and phenotype maps
    gene_map = {}
    pheno_map = {}
    for line in lines:
        if line.startswith('#'):
            continue
        tokens = line.split('\t')
        if not tokens or tokens == ['']:
            continue
        if tokens[1] == 'gene' or tokens[1] == 'gene/phenotype':
            if tokens[2]:
                gene_map[tokens[0]] = tokens[2]
        elif tokens[1] == 'phenotype' or tokens[1] == 'predominantly phenotypes':
            if tokens[2]:
                pheno_map[tokens[0]] = tokens[2]

    # HGNC map
    hgnc_map: Dict = get_hgnc_map(os.path.join(DATA_DIR, filename), 'Approved Gene Symbol (HGNC)')
    hgnc_map2: Dict = get_hgnc_map(os.path.join(DATA_DIR, filename2), 'Approved Gene Symbol')
    warning = 'Warning: MIM # {} was mapped to two different HGNC symbols, {} and {}. ' \
              'This was unexpected, so this mapping has been removed.'
    for mim_num, symbol in hgnc_map2.items():
        if mim_num not in hgnc_map:
            hgnc_map[mim_num] = symbol
        elif hgnc_map[mim_num] != symbol:
                LOG.warning(warning.format(mim_num, hgnc_map[mim_num], symbol))
                del hgnc_map[mim_num]

    return gene_map, pheno_map, hgnc_map


def parse_morbid_map(lines) -> Dict[str, List[str]]:
    """Parse morbid map file"""
    ret = {}
    p = re.compile(r".*,\s+(\d+)\s\(\d\)")
    for line in lines:
        if line.startswith('#'):
            continue
        tokens = line.split('\t')
        if not tokens or tokens == ['']:
            continue
        m = p.match(tokens[0])
        if m:
            phenotype_mim_number = m.group(1)
        else:
            phenotype_mim_number = ''
        gene_mim_number = tokens[2].strip()
        cyto_location = tokens[3].strip()
        ret[gene_mim_number] = [phenotype_mim_number, cyto_location]
    return ret


def get_maps_from_turtle() -> Tuple[Dict, Dict, Dict]:
    """This was created by Dazhi originally to read the prefixes. Generates a maps."""
    pmid_maps = defaultdict(list)
    umls_maps = defaultdict(list)
    orphanet_maps = defaultdict(list)
    mim_number = None

    with open(DATA_DIR / 'omim.ttl', 'r') as file:
        while line := file.readline():
            line = line.rstrip()
            if line.startswith('OMIM:'):
                mim_number = line.split()[0].split(':')[1]
            elif line.startswith('PMID:') or line.startswith('UMLS:') or line.startswith('@prefix'):
                continue
            else:
                pm_match = re.compile(r'.*PMID:(\d+).*').match(line)
                if pm_match:
                    pm_id = pm_match.group(1)
                    pmid_maps[mim_number].append(pm_id)
                umls_match = re.compile(r'.*UMLS:(C\d+).*').match(line)
                if umls_match:
                    umls_id = umls_match.group(1)
                    umls_maps[mim_number].append(umls_id)
                orphanet_match = re.compile(r'.*ORPHA:(C\d+).*').match(line)
                if orphanet_match:
                    orpha_id = orphanet_match.group(1)
                    orphanet_maps[mim_number].append(orpha_id)
    return pmid_maps, umls_maps, orphanet_maps


def get_updated_entries(start_year=2020, start_month=1, end_year=2021, end_month=8):
    """
    TODO: Update this function to dynamically retrieve the updated records
    :return:
    """
    # updated_mims = set()
    # updated_entries = []
    # for year in range(start_year, end_year):
    #     first_month = start_month if year == start_year else 1
    #     for month in range(first_month, 13):
    #         updated_mims |= set(get_codes_by_yyyy_mm(f'{year}/{month:02d}'))
    # for month in range(1, end_month + 1):
    #     updated_mims |= set(get_codes_by_yyyy_mm(f'{end_year}/{month:02d}'))
    # client = OmimClient(api_key=config['API_KEY'], omim_ids=list(updated_mims))
    # updated_entries.extend(client.fetch_all()['omim']['entryList'])
    with open(DATA_DIR / 'updated_01_2020_to_08_2021.json', 'r') as json_file:
        updated_entries = json.load(json_file)
    return updated_entries


def get_hgnc_symbol_id_map(input_path=os.path.join(DATA_DIR, 'hgnc', 'hgnc_complete_set.txt')) -> Dict[str, str]:
    """Get mapping between HGNC symbols and IDs
    todo: Ideally download the latest file: http://ftp.ebi.ac.uk/pub/databases/genenames/hgnc/tsv/hgnc_complete_set.txt
    todo: Address or suppress warning. I dont even need these columns anyway:
     /Users/joeflack4/projects/omim/omim2obo/main.py:208: DtypeWarning: Columns (32,34,38,40,50) have mixed types.Specify dtype option on import or set low_memory=False.
     hgnc_symbol_id_map: Dict = get_hgnc_symbol_id_map()
    """
    map = {}
    df = pd.read_csv(input_path, sep='\t')
    for index, row in df.iterrows():
        # hgnc_id is formatted as "hgnc:<id>"
        map[row['symbol']] = row['hgnc_id'].split(':')[1]

    return map
