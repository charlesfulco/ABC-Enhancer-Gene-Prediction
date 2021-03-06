import pandas as pd
import numpy as np
import os
import os.path
from subprocess import check_call, check_output, PIPE, Popen, getoutput, CalledProcessError
from intervaltree import IntervalTree
from pyBigWig import open as open_bigwig
# import pysam
from tools import *
import linecache
import traceback
import time

pd.options.display.max_colwidth = 10000 #seems to be necessary for pandas to read long file names... strange

def load_genes(file,
               ue_file,
               outdir,
               expression_table_list,
               gene_id_names,
               primary_id):

    bed = read_bed(file) 
    genes = process_gene_bed(bed, gene_id_names, primary_id)

    genes[['chr', 'start', 'end', 'name', 'score', 'strand']].to_csv(os.path.join(outdir, "GeneList.bed"),
                                                                    sep='\t', index=False, header=False)

    if len(expression_table_list) > 0:
        # # Add expression information

        names_list = []
        print("Using gene expression from files: {}".format(expression_table_list))

        for expression_table in expression_table_list:
            try:
                name = os.path.basename(expression_table)
                expr = pd.read_table(expression_table, names=[primary_id, name + '.Expression'])
                expr[name + '.Expression'] = expr[name + '.Expression'].astype(float)
                expr = expr.groupby(primary_id).max()
                expr['symbol'] = expr.index.values

                genes = genes.merge(expr, how="left", on=primary_id)
                names_list.append(name + '.Expression')
            except Exception as e:
                print(e)
                traceback.print_exc()
                print("Failed on {}".format(expression_table))

        genes['Expression'] = genes[names_list].mean(axis = 1)
        genes['Expression.quantile'] = genes['Expression'].rank(method='average', na_option="top", ascending=True, pct=True)
    else:
        genes['Expression'] = np.NaN
    
    #Ubiquitously expressed annotation
    ubiq = pd.read_csv(ue_file, sep="\t")
    genes['is_ue'] = genes['name'].isin(ubiq.iloc[:,0].values.tolist())

    return genes


def annotate_genes_with_features(genes, genome, 
               skip_gene_counts=False,
               features={},
               outdir=".",
               force=False,
               **kwargs):

    #file = genome['genes']
    genome_sizes = genome['sizes']
    bounds_bed = os.path.join(outdir, "GeneList.bed")
    #tss1kb_file = file + '.TSS1kb.bed'

    #Make bed file with TSS +/- 500bp
    tss1kb = genes.ix[:,['chr','start','end','name','score','strand']]
    tss1kb['start'] = genes['tss'] - 500
    tss1kb['end'] = genes['tss'] + 500
    tss1kb_file = os.path.join(outdir, "GeneList.TSS1kb.bed")
    tss1kb.to_csv(tss1kb_file, header=False, index=False, sep='\t')
    
    # if not os.path.isfile(tss1kb_file):
    #     tss1kb = genes.ix[:,['chr','start','end','symbol','score','strand']]
    #     tss1kb['start'] = genes['tss'] - 500
    #     tss1kb['end'] = genes['tss'] + 500
    #     tss1kb.to_csv(tss1kb_file, header=False, index=False, sep='\t')
    # else:
    #     tss1kb = read_bed(tss1kb_file)

    genes = count_features_for_bed(genes, bounds_bed, genome_sizes, features, outdir, "Genes", force=force)
    tsscounts = count_features_for_bed(tss1kb, tss1kb_file, genome_sizes, features, outdir, "Genes.TSS1kb", force=force)
    tsscounts = tsscounts.drop(['chr','start','end','score','strand'], axis=1)

    # import pdb
    # pdb.set_trace()

    merged = genes.merge(tsscounts, on="name", suffixes=['','.TSS1Kb'])

    access_col = kwargs["default_accessibility_feature"] + ".RPKM.quantile.TSS1Kb"  
    merged['PromoterActivityQuantile'] = ((0.0001+merged['H3K27ac.RPKM.quantile.TSS1Kb'])*(0.0001+merged[access_col])).rank(method='average', na_option="top", ascending=True, pct=True)
    return merged

def process_gene_bed(bed, name_cols, main_name):

    try:
        bed = bed.drop(['thickStart','thickEnd','itemRgb','blockCount','blockSizes','blockStarts'], axis=1)
    except Exception as e:
        pass
    
    assert(main_name in name_cols)

    names = bed.name.str.split(";", expand=True)
    names.columns = name_cols.split(",")
    assert(len(names.columns) == len(name_cols.split(",")))
    bed = pandas.concat([bed, names], axis=1)

    bed['name'] = bed[main_name]
    bed = bed.sort_values(by=['chr','start'])

    bed['tss'] = get_tss_for_bed(bed)

    bed.drop_duplicates(inplace=True)

    return bed

def get_tss_for_bed(bed):
    assert_bed3(bed)
    tss = bed['start'].copy()
    tss.ix[bed.loc[:,'strand'] == "-"] = bed.ix[bed.loc[:,'strand'] == "-",'end']
    return tss

def assert_bed3(df):
    assert(type(df).__name__ == "DataFrame")
    assert('chr' in df.columns)
    assert('start' in df.columns)
    assert('end' in df.columns)

def load_enhancers(outdir=".",
                   genome_sizes="",
                   features={},
                   genes=None,
                   force=False,
                   active_col="",
                   quantile_cutoff=0.5,
                   candidate_peaks="",
                   skip_originalPeaks=False,
                   compute_custom_features=False,
                   skip_rpkm_quantile=False,
                   cellType="",
                   additional_gene_annot=None,
                   tss_slop_for_class_assignment = 500,
                   **kwargs):

    enhancers = read_bed(candidate_peaks)
    enhancers = enhancers.ix[~ (enhancers.chr.str.contains(re.compile('random|chrM|_|hap|Un')))]

    enhancers = count_features_for_bed(enhancers, candidate_peaks, genome_sizes, features, outdir, "Enhancers", skip_rpkm_quantile, force)

    #compute custom features
    # if compute_custom_features:
    #     enhancers = run_compute_custom_features(enhancers, cellType)

    # Assign categories
    if genes is not None:
        print("Assigning classes to enhancers")
        enhancers = assign_enhancer_classes(enhancers, genes, tss_slop = tss_slop_for_class_assignment)

        # Output stats
        print("Total enhancers: {}".format(len(enhancers)))
        print("            Promoters: {}".format(sum(enhancers['isPromoterElement'])))
        print("          Genic: {}".format(sum(enhancers['isGenicElement'])))
        print("         Intergenic: {}".format(sum(enhancers['isIntergenicElement'])))

    return enhancers


# def load_domains(*args, **kwargs):
#     pass

def assign_enhancer_classes(enhancers, genes, tss_slop=500):
    # build interval trees
    tss_intervals = {}
    gene_intervals = {}
    for chr, chrdata in genes.groupby('chr'):
        tss_intervals[chr] = IntervalTree.from_tuples(zip(chrdata.tss - tss_slop, chrdata.tss + tss_slop,
                                                          [str(x) for x in chrdata.symbol]))
        gene_intervals[chr] = IntervalTree.from_tuples(zip(chrdata.start, chrdata.end))

    # if additional_tss is not None:
    #     additional = pd.read_csv(additional_tss, sep="\t")
    #     additional = additional.loc[additional['cellType'] == cellType, :] 

    #     for idx, row in additional.iterrows():
    #         tss_intervals[row['chr']].addi(row['tss'] - tss_slop, row['tss'] + tss_slop, row['tss_name'])

    def get_class(enhancer):
        start, end = sorted((enhancer.start, enhancer.end))
        if tss_intervals[enhancer.chr][start:end]:
            return "promoter"
        if gene_intervals[enhancer.chr][start:end]:
            return "genic"
        return "intergenic"

    def get_tss_symbol(enhancer):
        #For candidate regions that overlap gene promoters, annotate enhancers data table with the name of the gene.
        if enhancer["class"] == "promoter":
            start, end = sorted((enhancer.start, enhancer.end))
            overlaps = tss_intervals[enhancer.chr][start:end]
            return ",".join(list(set([o[2] for o in overlaps])))
        return ""

    enhancers["class"] = enhancers.apply(get_class, axis=1)
    enhancers["isPromoterElement"] = enhancers["class"] == "promoter"
    enhancers["isGenicElement"] = enhancers["class"] == "genic"
    enhancers["isIntergenicElement"] = enhancers["class"] == "intergenic"
    enhancers["enhancerSymbol"] = enhancers.apply(get_tss_symbol, axis=1)
    assert (enhancers.enhancerSymbol == "\n").sum() == 0
    enhancers["name"] = enhancers.apply(lambda e: "{}|{}:{}-{}".format(e["class"], e.chr, e.start, e.end), axis=1)
    return(enhancers)

def run_count_reads(target, output, bed_file, genome_sizes):
    if target.endswith(".bam"):
        count_bam(target, bed_file, output, genome_sizes=genome_sizes)
    elif target.endswith(".tagAlign.gz") or target.endswith(".tagAlign.bgz"):
        count_tagalign(target, bed_file, output, genome_sizes)
    elif isBigWigFile(target):
        count_bigwig(target, bed_file, output)
    else:
        raise ValueError("File {} name was not in .bam, .tagAlign.gz, .bw".format(target))


def count_bam(bamfile, bed_file, output, genome_sizes, use_java=False, use_fast_count=True):
    completed = True
    if not use_java:        
        #Fast count:
        #bamtobed uses a lot of memory. Instead reorder bed file to match ordering of bam file. Assumed .bam file is sorted in the chromosome order defined by its header.
        #Then use bedtools coverage, then sort back to expected order
        #Requires an faidx file with chr in the same order as the bam file.
        if use_fast_count:
            temp_output = output + ".temp_sort_order"
            faidx_command = reuse(".samtools-0.1.19") + "awk 'FNR==NR {{x2[$1] = $0; next}} $1 in x2 {{print x2[$1]}}' {genome_sizes} <(samtools view -H {bamfile} | grep SQ | cut -f 2 | cut -c 4- )  > {temp_output}".format(**locals())
            command = reuse(".bedtools-2.26.0") + "bedtools sort -faidx {temp_output} -i {bed_file} | bedtools coverage -g {temp_output} -counts -sorted -a stdin -b {bamfile} | awk '{{print $1 \"\\t\" $2 \"\\t\" $3 \"\\t\" $NF}}'  | bedtools sort -faidx {genome_sizes} -i stdin > {output}; rm {temp_output}".format(**locals())

            #executable='/bin/bash' needed to parse < redirect in faidx_command
            p = Popen(faidx_command, stdout=PIPE, stderr=PIPE, shell=True, executable='/bin/bash')
            print("Running: " + faidx_command)
            (stdoutdata, stderrdata) = p.communicate()
            err = str(stderrdata, 'utf-8')

            p = Popen(command, stdout=PIPE, stderr=PIPE, shell=True)
            print("Running: " + command)
            (stdoutdata, stderrdata) = p.communicate()
            err = str(stderrdata, 'utf-8')

            try:
                data = pd.read_table(output, header=None).ix[:,3].values
            except Exception as e:
                print("Fast count method failed to count: " + str(bamfile) + "\n")
                print(err)
                print("Trying bamtobed method ...\n")
                completed = False

        # Replace Java with BEDTools: convert BAM to BED, filter to standard chromosomes, sort, then use the very fast bedtools coverage -sorted algorithm
        # Note: This requires that bed_file is also sorted and in same chromosome order as genome_sizes (first do bedtools sort -i bed_file -faidx genome_sizes)
        #         BEDTools will error out if files are not properly sorted
        # Also requires that {genome_sizes} has a corresponding {genome_sizes}.bed file
        if not use_fast_count or ("terminated"  in err) or ("Error" in err) or ("ERROR" in err) or not completed:
            command = reuse(".bedtools-2.26.0") + "bedtools bamtobed -i {bamfile} | cut -f 1-3 | bedtools intersect -wa -a stdin -b {genome_sizes}.bed | bedtools sort -i stdin -faidx {genome_sizes} | bedtools coverage -g {genome_sizes} -counts -sorted -a {bed_file} -b stdin | awk '{{print $1 \"\\t\" $2 \"\\t\" $3 \"\\t\" $NF}}' > {output}".format(**locals())
            p = Popen(command, stdout=PIPE, stderr=PIPE, shell=True)
            print("Running: " + command)
            (stdoutdata, stderrdata) = p.communicate()

            try:
                data = pd.read_table(output, header=None).ix[:,3].values
            except Exception as e:
                print(e)
                print(stderrdata)
                completed = False

        # Check for successful finish -- BEDTools can run into memory problems
        #import pdb; pdb.set_trace()
        err = str(stderrdata, 'utf-8')
        if ("terminated" not in err) and ("Error" not in err) and ("ERROR" not in err) and any(data):
            print("BEDTools completed successfully. \n")
            completed = True
        else:
            print("BEDTools failed to count file: " + str(bamfile) + "\n")
            print(err)
            print("Trying using Java method ...\n")
            completed = False

    # Slow JAVA counting
    # if use_java or not completed:
    #     print("Running Java method ...\n")
    #     command = (reuse("Java-1.7") + \
    #                 "java -Xmx8g -cp /seq/lincRNA/Jesse/bin/scripts/Nextgen_140308.jar broad.pda.seq.rap.CountReads "
    #                 "TARGET={bamfile} OUTPUT={output}.tmp SCORE=count ANNOTATION_FILE={bed_file} MASK_FILE=null PAIRED_END=false "
    #                 "VALIDATION_STRINGENCY=LENIENT SIZES={genome_sizes} MIN_MAPPING_QUALITY=0").format(**locals())
    #     run_command(command)
    #     # Convert output to bedgraph format (chr,start,end,score -- no header)
    #     command2 = "cut -f 1-3,5 {output}.tmp | sed 's/\.0//' > {output}; rm {output}.tmp".format(**locals())
    #     run_command(command2)



def count_tagalign(tagalign, bed_file, output, genome_sizes):
    # import pdb
    # pdb.set_trace()


    #JN 3/16/18, faster and more memory efficient. Dont subset original tag align file.
    #JN 8/31/18: actually probably slower, but more memory efficient
    # print("trying fast sorted method")
    # fast_cmd = reuse(".bedtools-2.26.0") + "bedtools sort -faidx {genome_sizes} -i {tagalign}| bedtools coverage -counts -sorted -g {genome_sizes} -a {bed_file} -b stdin | awk '{{print $1 \"\\t\" $2 \"\\t\" $3 \"\\t\" $NF}}' > {output}".format(**locals())

    # p = Popen(fast_cmd, stdout=PIPE, stderr=PIPE, shell=True)
    # print("Running: " + fast_cmd)
    # (stdoutdata, stderrdata) = p.communicate()
    # err = str(stderrdata, 'utf-8')

    # if ("terminated" not in err) and ("Error" not in err) and ("ERROR" not in err):
    #     print("Fast method ran successfully")
    # else:
    # print(err)
    # print("Trying unsorted method")
    command1 = reuse("Tabix") + "tabix -B {tagalign} {bed_file} | cut -f1-3".format(**locals())
    command2 = reuse(".bedtools-2.26.0") + "bedtools coverage -counts -b stdin -a {bed_file} | awk '{{print $1 \"\\t\" $2 \"\\t\" $3 \"\\t\" $NF}}' ".format(**locals())
    p1 = Popen(command1, stdout=PIPE, shell=True)
    with open(output, "wb") as outfp:
        p2 = check_call(command2, stdin=p1.stdout, stdout=outfp, shell=True)

    if not p2 == 0:
        print(p2.stderr)

def count_bigwig(target, bed_file, output):
    bw = open_bigwig(target)
    bed = read_bed(bed_file)
    with open(output, "wb") as outfp:
        for chr, start, end, *rest in bed.itertuples(index=False, name=None):
            # if isinstance(name, np.float):
            #     name = ""
            try:
                val = bw.stats(chr, int(start), int(max(end, start + 1)), "mean")[0] or 0
            except RuntimeError:
                print("Failed on", chr, start, end)
                raise
            val *= abs(end - start)  # convert to total coverage
            output = ("\t".join([chr, str(start), str(end), str(val)]) + "\n").encode('ascii')
            outfp.write(output)


def isBigWigFile(filename):
    return(filename.endswith(".bw") or filename.endswith(".bigWig") or filename.endswith(".bigwig"))

def count_features_for_bed(df, bed_file, genome_sizes, features, directory, filebase, skip_rpkm_quantile=False, force=False):

    for feature, feature_bam_list in features.items():
        start_time = time.time()
        if isinstance(feature_bam_list, str): 
            feature_bam_list = [feature_bam_list]

        for feature_bam in feature_bam_list:
            df = count_single_feature_for_bed(df, bed_file, genome_sizes, feature_bam, feature, directory, filebase, skip_rpkm_quantile, force)

        df = average_features(df, feature.replace('feature_',''), feature_bam_list, skip_rpkm_quantile)
        elapsed_time = time.time() - start_time
        print("Feature " + feature + " completed in " + str(elapsed_time))

    return df

def count_single_feature_for_bed(df, bed_file, genome_sizes, feature_bam, feature, directory, filebase, skip_rpkm_quantile, force):
    orig_shape = df.shape[0]
    feature_name = feature + "." + os.path.basename(feature_bam)
    feature_outfile = os.path.join(directory, "{}.{}.CountReads.bed".format(filebase, feature_name))

    if force or (not os.path.exists(feature_outfile)) or (os.path.getsize(feature_outfile) == 0):
        print("Regenerating", feature_outfile)
        print("Counting coverage for {}".format(filebase + "." + feature_name))
        run_count_reads(feature_bam, feature_outfile, bed_file, genome_sizes)
    else:
        print("Loading coverage from pre-calculated file for {}".format(filebase + "." + feature_name))

    domain_counts = read_bed(feature_outfile)
    score_column = domain_counts.columns[-1]

    total_counts = count_total(feature_bam)

    domain_counts = domain_counts[['chr', 'start', 'end', score_column]]
    featurecount = feature_name + ".readCount"
    domain_counts.rename(columns={score_column: featurecount}, inplace=True)

    df = df.merge(domain_counts.drop_duplicates())
    #df = smart_merge(df, domain_counts.drop_duplicates())

    assert df.shape[0] == orig_shape

    df[feature_name + ".RPM"] = 1e6 * df[featurecount] / float(total_counts)

    if not skip_rpkm_quantile:
        df[featurecount + ".quantile"] = df[featurecount].rank() / float(len(df))
        df[feature_name + ".RPM.quantile"] = df[feature_name + ".RPM"].rank() / float(len(df))
        df[feature_name + ".RPKM"] = 1e3 * df[feature_name + ".RPM"] / (df.end - df.start).astype(float)
        df[feature_name + ".RPKM.quantile"] = df[feature_name + ".RPKM"].rank() / float(len(df))

    return df[~ df.duplicated()]

def average_features(df, feature, feature_bam_list, skip_rpkm_quantile):
    feature_RPM_cols = [feature + "." + os.path.basename(feature_bam) + '.RPM' for feature_bam in feature_bam_list]

    df[feature + '.RPM'] = df[feature_RPM_cols].mean(axis = 1)
    
    if not skip_rpkm_quantile:
        feature_RPKM_cols = [feature + "." + os.path.basename(feature_bam) + '.RPKM' for feature_bam in feature_bam_list]
        df[feature + '.RPM.quantile'] = df[feature + '.RPM'].rank() / float(len(df))
        df[feature + '.RPKM'] = df[feature_RPKM_cols].mean(axis = 1)
        df[feature + '.RPKM.quantile'] = df[feature + '.RPKM'].rank() / float(len(df))

    return df

# From /seq/lincRNA/Jesse/bin/scripts/JuicerUtilities.R
#
bed_extra_colnames = ["name", "score", "strand", "thickStart", "thickEnd", "itemRgb", "blockCount", "blockSizes", "blockStarts"]
chromosomes = ['chr' + str(entry) for entry in list(range(1,23)) + ['M','X','Y']]   # should pass this in as an input file to specify chromosome order
def read_bed(filename, extra_colnames=bed_extra_colnames, chr=None, sort=False, skip_chr_sorting=False):
    skip = 1 if ("track" in open(filename, "r").readline()) else 0
    names = ["chr", "start", "end"] + extra_colnames
    result = pd.read_table(filename, names=names, header=None, skiprows=skip, comment='#')
    result = result.dropna(axis=1, how='all')  # drop empty columns
    assert result.columns[0] == "chr"

    result['chr'] = pd.Categorical(result['chr'], chromosomes, ordered=True)
    if chr is not None:
        result = result[result.chr == chr]
    if not skip_chr_sorting:
        result.sort_values("chr", inplace=True)
    if sort:
        result.sort_values(["chr", "start", "end"], inplace=True)
    return result


def read_bedgraph(filename):
    read_bed(filename, extra_colnames=["score"], skip_chr_sorting=True)


# def read_count_reads(file, chr=None, sort=False):
#     return read_bed(file, chr=chr, sort=sort,
#                     extra_colnames=bed_extra_colnames + ["count", "rpkm", "regionTotal", "total", "length"])


def count_bam_mapped(bam_file):
    # Counts number of reads in a BAM file WITHOUT iterating.  Requires that the BAM is indexed
    chromosomes = ['chr' + str(x) for x in range(1,23)] + ['chrX'] + ['chrY']
    command = (reuse(".samtools-0.1.19") + "samtools idxstats " + bam_file)
    data = check_output(command, shell=True)
    lines = data.decode("ascii").split("\n")
    vals = list(int(l.split("\t")[2]) for l in lines[:-1] if l.split("\t")[0] in chromosomes)
    if not sum(vals) > 0:
        raise ValueError("Error counting BAM file: count <= 0")
    return sum(vals)

def count_tagalign_total(tagalign):
    #result = int(check_output("zcat " + tagalign + " | wc -l", shell=True))
    result = int(check_output("zcat {} | grep -E 'chr[1-9]|chr1[0-9]|chr2[0-2]|chrX|chrY' | wc -l".format(tagalign), shell=True))
    assert (result > 0)
    return result

def count_bigwig_total(bw_file):
    bw = open_bigwig(bw_file)
    result = sum(l * bw.stats(ch, 0, l, "mean")[0] for ch, l in bw.chroms().items())
    assert (abs(result) > 0)  ## BigWig could have negative values, e.g. the negative-strand GroCAP bigwigs
    return result

def count_total(infile):
    if infile.endswith(".tagAlign.gz") or infile.endswith(".tagAlign.bgz"):
        total_counts = count_tagalign_total(infile)
    elif infile.endswith(".bam"):
        total_counts = count_bam_mapped(infile)
    elif isBigWigFile(infile):
        total_counts = count_bigwig_total(infile)
    else:
        raise RuntimeError("Did not recognize file format of: " + infile)

    return total_counts

def make_features_from_param_df(df, supp=None):
    # import pdb
    # pdb.set_trace()

    features = {}

    features['H3K27ac'] = df['feature_H3K27ac'].to_string(index=False).split(",")

    #Note that even if ATAC is not the default accessibilty feature, we still want to count it, as long as there is an ATAC file
    if ('feature_ATAC' in df.columns) and (not all(df['feature_ATAC'].isnull())):
        features['ATAC'] = df['feature_ATAC'].to_string(index=False).split(",")

    if ('feature_DHS' in df.columns) and not all(df['feature_DHS'].isnull()):
        features['DHS'] = df['feature_DHS'].to_string(index=False).split(",")

    if supp is not None:
        #supp = pd.read_csv(supp_file, sep="\t")
        for idx,row in supp.iterrows():

            #Checks to make sure supplemental feature is not df already. This is useful for H3K27ac so it doesn't appear twice
            if row['feature_name'] not in features.keys():
                features[row['feature_name']] = row['file'].split(",")
            else:
                print("{} Already exists. Skipping adding as supplemental feature!".format(row['feature_name']))

    return features

def parse_params_file(cellType, args):
    # Parse parameters file and return params dictionary
    params_from_file = pd.read_csv(args.params_file, sep="\t")
    params_df = params_from_file.loc[params_from_file["cell_type"] == cellType, :]

    params = {}
    params["genome_build"] = params_df["genome"].values[0]
    params["quantile_cutoff"] = 0
    params["default_accessibility_feature"] = params_df["default_accessibility_feature"].values[0]
    #params["expression_rpkm_cutoff"] = args.expression_rpkm_cutoff
    
    #params["candidate_region_file"] = params_df["candidate_region_file"].values[0]
    params["features"] = make_features_from_param_df(params_df)
    #params["features"] = make_features_from_param_df(params_df, supplemental_feature_df)

    #RNA Seq    
    if (params_df["RNA_tpm_file"].tolist() == 'NA' or params_df["RNA_tpm_file"].isnull().values.all()):
        params["expression_table"] = ''
    else:
        params["expression_table"] = params_df["RNA_tpm_file"].tolist()[0].split(",")

    return(params)

# def choose_candidate_region_file(params_df, args, sort_col="read_count"):
#     #If candidate regions are provided then just use that.
#     #Otherwise, pick the best peak calls from the replicates DHS or ATAC files

#     if "chosen_region_file" in params.keys() and params["chosen_region_file"] is not None and (not params["chosen_region_file"].isnull().values[0]):
#         chosen_region_file = params["chosen_region_file"].values[0]
#     else:
#         feature_stats = pd.read_csv(os.path.join(args.feature_directory, "feature.stats.txt"), sep="\t")
#         access_feature = feature_stats["default_accessibility_feature"].values[0]

#         if (feature_stats.loc[feature_stats["feature"] == access_feature, ]).shape[0] == 1:
#             chosen_region_file = feature_stats['candidate_region_file'][0]
#         else:            
#             assert(len(set(feature_stats["default_accessibility_feature"])) == 1)

#             #temp = feature_stats.loc[np.logical_and(feature_stats["feature"] == access_feature, feature_stats["total_count"] >= MIN_READS)]
#             temp = feature_stats.loc[feature_stats["feature"] == access_feature]

#             if temp.shape[0] == 1:
#                 return temp["candidate_region_file"].values[0]

#             best_idx = temp[sort_col].idxmax()
#             chosen_region_file = temp["candidate_region_file"][best_idx]

#     return chosen_region_file
