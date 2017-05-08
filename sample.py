import collections
import logging
import numpy
import pysam
import random
import sys

from kde import gaussian_kde
import mapq

logger = logging.getLogger(__name__)




class Sample(object):
    def __init__(self, name, bam_path):
        self.name = name

        self.single_ended = False
        self.orientations = None
        self.search_distance = None

        self.bam_path = bam_path
        self._bam = None

        self.outbam = None

        self.read_statistics = ReadStatistics(self.bam)
        if self.read_statistics.orientations == "any":
            self.single_ended = True

        self.sequencer = "illumina"
        if self.single_ended:
            mismatches = numpy.mean(self.read_statistics.number_mismatches)
            lengths = numpy.mean(self.read_statistics.readLengths)
            mismatch_rate = mismatches / lengths

            # these error rates aren't really accurate in terms of describing the 
            # two platforms anymore, but they do correspond to the presets that
            # bwa mem has, which we're mimicking
            if mismatch_rate > 0.10:
                self.sequencer = "minion"
            elif mismatch_rate > 0.01:
                self.sequencer = "pacbio"

        print("ALIGNMENT PARAMS:::", self.sequencer)


    @property
    def bam(self):
        if self._bam is None:
            self._bam = pysam.AlignmentFile(self.bam_path, "rb")
            try:
                self._bam.fetch()
            except ValueError:
                logger.error("ERROR: Need to create index for input bam file: {}".format(self.bam_path))
                sys.exit(0)
        return self._bam

    # def set_bwa_params(self, realigner):
    #     for bwa in [realigner.ref_mapper, realigner.alt_mapper]:
    #         if self._sequencer == "illumina":
    #             set_illumina_params(bwa)
    #         elif self._sequencer == "pacbio":
    #             set_pacbio_params(bwa)
    #         elif self._sequencer == "minion":
    #             set_minion_params(bwa)


    def __getstate__(self):
        """ allows pickling of Samples()s """
        state = self.__dict__.copy()
        del state["bam"]
        return state


class ReadStatistics(object):
    def __init__(self, bam):
        self.insertSizes = []
        self.readLengths = []
        self.orientations = []
        self.number_mismatches = []

        self._insertSizeKDE = None
        self.singleEnded = False

        self._insertSizeScores = {} # cache
        self._max_insert_size = None

        try:
            results = sampleInsertSizes(bam)
            self.insertSizes, self.orientations, self.readLengths, self.number_mismatches, self.discordant_frac = results
            if len(self.insertSizes) > 1:
                logger.info("  insert size mean: {:.2f} std: {:.2f} min:{} max:{}".format(
                    numpy.mean(self.insertSizes), numpy.std(self.insertSizes),
                    numpy.min(self.insertSizes), self.maxInsertSize()))
                logger.info("  discordant: {:.4f}".format(self.discordant_frac))
        except Exception as e:
            logger.error("Error determining orientation / pairing statistics: {}".format(e))


    def score_read_pair(self, pair):
        if not pair.concordant(self):
            pair.score = pair.aln1.score + pair.aln2.score + -10
            return

        insert_size_prob = self.scoreInsertSize(pair.insert_size)

        with numpy.errstate(divide="ignore"):
            log10_pair_prob = numpy.log10(insert_size_prob) + pair.aln1.score + pair.aln2.score

        # print("PAIR:", score1, score2, pair_prob, prob_to_phred(pair_prob, 10))
        # print(pair_prob, prob_to_phred(pair_prob, 10))

        # return log10_pair_prob

        pair.score = log10_pair_prob

    def scoreInsertSize(self, isize):
        if not self.hasInsertSizeDistribution():
            return 0

        if self._insertSizeKDE is None:
            self._insertSizeKDE = gaussian_kde(self.insertSizes)

        # the gaussian kde call is pretty slow with ~50,000 data points in it, so we'll cache the result for a bit of a speed-up
        isize = abs(isize)
        if not isize in self._insertSizeScores:
            self._insertSizeScores[isize] = self._insertSizeKDE(isize)[0]

        return self._insertSizeScores[isize]


    def hasInsertSizeDistribution(self):
        if len(self.insertSizes) > 1000:
            return True
        return False

    def maxInsertSize(self):
        if self._max_insert_size is None and self.hasInsertSizeDistribution():
            self._max_insert_size = numpy.max(self.insertSizes)
        return self._max_insert_size

    def meanInsertSize(self):
        if self.hasInsertSizeDistribution():
            return numpy.mean(self.insertSizes)
        return None

    def stddevInsertSize(self):
        if self.hasInsertSizeDistribution():
            return numpy.std(self.insertSizes)
        return None


    def hasReadLengthDistribution(self):
        if len(self.readLengths) > 1000:
            return True
        return False

    def meanReadLength(self):
        if self.hasReadLengthDistribution():
            return numpy.mean(self.readLengths)
        return None

    def stddevReadLength(self):
        if self.hasReadLengthDistribution():
            return numpy.std(self.readLengths)
        return None

    def readLengthUpperQuantile(self):
        if self.hasReadLengthDistribution():
            return numpy.percentile(self.readLengths, 99)
        return None


def removeOutliers(data, m = 10.):
    """ a method of trimming outliers from a list/array using 
    outlier-safe methods of calculating the center and variance;
    only removes the upper tail, not the lower tail """
    if len(data) < 2:
        return data
        
    data = numpy.array(data)
    d_abs = numpy.abs(data - numpy.median(data))
    d = data - numpy.median(data)
    mdev = numpy.median(d_abs)
    s = d/mdev if mdev else 0.
    return data[s<m]

def chooseOrientation(orientations):
    logger.info("  counts +/-:{:<6} -/+:{:<6} +/+:{:<6} -/-:{:<6} unpaired:{:<6}".format(orientations[False, True], 
                                                    orientations[True, False], 
                                                    orientations[True, True],
                                                    orientations[False, False],
                                                    orientations["unpaired"]))
    ranked = sorted(orientations, key=lambda x: orientations[x])
    chosenOrientations = [ranked.pop()]
    while len(ranked) > 0:
        candidate = ranked.pop()
        if orientations[chosenOrientations[-1]] < 2* orientations[candidate]:
            chosenOrientations.append(candidate)
        else:
            break
    if chosenOrientations[0] == "unpaired":
        chosenOrientations = "any"
    else:
        d = {False: "+", True:"-"}
        chosenOrientations = ["".join(d[x] for x in o) for o in chosenOrientations]
    return chosenOrientations

def getSearchRegions(bam, minLength=0):
    # get the chromosomes and move X, Y, M/MT to the end
    chromosomes = []
    chrom_lengths = dict((bam.getrname(i),bam.lengths[i]) for i in range(bam.nreferences))

    for chrom in chrom_lengths:
        if chrom_lengths[chrom] > minLength:
            chromosomes.append(chrom)
    # for i in range(bam.nreferences):
    #     if bam.lengths[i] > minLength:
    #         chromosomes.append(bam.getrname(i))

    ideal_start = 2500000

    regions = []
    for chrom in sorted(chromosomes):
        for start in range(ideal_start, chrom_lengths[chrom]-ideal_start, 1000000):
            regions.append((chrom, start, start+10000))

    rand = random.Random()
    rand.seed(9535)
    rand.shuffle(regions)
    for region in regions:
        yield region

    for chrom in sorted(chromosomes):
        yield (chrom, None, None)


def sampleInsertSizes(bam, maxreads=50000, skip=0, minmapq=40, reference=None):
    """ get the insert size distribution, cutting off the tail at the high end, 
    and removing most oddly mapping pairs

    50,000 reads seems to be sufficient to get a nice distribution, and higher values
        don't tend to change the distribution much """

    inserts = []
    readLengths  = []
    nms = []
    
    count = 0

    orientations = collections.Counter()

    if reference is not None:
        mapq_calculator = mapq.MAPQCalculator(reference)

    def tally_nm(_read):
        if reference is not None and not _read.has_tag("NM"):
            mapq_calculator.get_alignment_end_score(_read)
        if _read.has_tag("NM"):
            nms.append(_read.get_tag("NM"))

    discordant = 0
    concordant = 0

    for chrom, start, end in getSearchRegions(bam):
        for read in bam.fetch(chrom, start, end):
            if skip > 0:
                skip -= 1
                continue

            if orientations["unpaired"] > 2500 and count < 1000:
                # bail out early if it looks like it's single-ended
                break


            if not read.is_paired:
                orientations["unpaired"] += 1
                readLengths.append(len(read.seq))
                tally_nm(read)
                continue
                
            if not read.is_read1:
                continue
            if read.is_unmapped or read.mate_is_unmapped:
                continue
            if read.is_secondary or read.is_supplementary:
                continue
            if not read.is_proper_pair:
                discordant += 1
                continue
            else:
                concordant += 1

            if read.mapq < minmapq:
                continue
            if read.tid != read.rnext:
                continue

            inserts.append(abs(read.isize))

            curOrient = (read.is_reverse, read.mate_is_reverse)
            if read.reference_start > read.next_reference_start:
                curOrient = not curOrient[0], not curOrient[1]
            orientations[curOrient] += 1
            readLengths.append(len(read.seq))

            tally_nm(read)

            count += 1
            if count > maxreads:
                break
        if count >= maxreads:
            break

    chosenOrientations = chooseOrientation(orientations)

    # print("NM "*30, numpy.mean(nms), numpy.mean(readLengths),
    #     numpy.median(numpy.array(nms)/numpy.array(readLengths, dtype=float)))
    return removeOutliers(inserts), chosenOrientations, numpy.array(readLengths), numpy.array(nms), discordant/float(discordant+concordant)
