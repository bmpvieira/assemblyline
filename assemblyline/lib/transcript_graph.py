'''
Created on Feb 13, 2011

@author: mkiyer
'''
import networkx as nx
import logging
import collections
import itertools
import operator
import bisect

from bx.intersection import Interval, IntervalTree
from bx.cluster import ClusterTree
from base import Exon, POS_STRAND, NEG_STRAND, NO_STRAND, cmp_strand, strand_int_to_str

from assembler import assemble_transcript_graph

class TranscriptData(object):
    __slots__ = ('id', 'strand', 'score')
    def __init__(self, id, strand, score):
        self.id = id
        self.strand = strand
        self.score = float(score)
    def __repr__(self):
        return ("<%s(id='%s',strand='%s',score='%s')>" % 
                (self.__class__.__name__, self.id, self.strand, 
                 str(self.score))) 
    def __str__(self):
        return ("[id=%s,strand=%s,score=%.2f]" % 
                (self.id, strand_int_to_str(self.strand), self.score))

def find_intron_starts_and_ends(transcripts):
    '''
    input: list of Transcript objects
    output: intron starts/ends by strand (list to index strand, list of starts)    
    
    list of lists of intron starts, list of lists of intron ends
    '''
    # TODO: combine 'find_exon_boundaries' with this
    intron_starts = [set(), set(), set()]
    intron_ends = [set(), set(), set()]
    for transcript in transcripts:
        exons = transcript.exons
        strand = transcript.strand
        if len(exons) == 1:
            continue
        if transcript.strand == NO_STRAND:
            logging.warning("Multi-exonic unstranded transcript detected!")
            continue
        intron_starts[strand].add(exons[0].end)
        for e in transcript.exons[1:-1]:
            intron_starts[strand].add(e.end)
            intron_ends[strand].add(e.start)
        intron_ends[strand].add(exons[-1].start)
    # combine pos/neg to get unstranded
    intron_starts[NO_STRAND] = intron_starts[POS_STRAND].union(intron_starts[NEG_STRAND])
    intron_ends[NO_STRAND] = intron_ends[POS_STRAND].union(intron_ends[NEG_STRAND])
    # sort lists
    for strand in (POS_STRAND, NEG_STRAND, NO_STRAND):
        intron_starts[strand] = sorted(intron_starts[strand])
        intron_ends[strand] = sorted(intron_ends[strand])
    return intron_starts, intron_ends

def trim_left(start, end, intron_starts, intron_ends, overhang_threshold):
    trim_start = start
    # search for the nearest intron end greater than
    # or equal to the current exon start
    i = bisect.bisect_left(intron_ends, start)
    if i != len(intron_ends):
        nearest_intron_end = intron_ends[i]
        # cannot trim past end of exon and cannot
        # trim more than the overhang threshold
        if ((nearest_intron_end < end) and
            (nearest_intron_end <= start + overhang_threshold)):
            trim_start = nearest_intron_end
    return trim_start

def trim_right(start, end, intron_starts, intron_ends, overhang_threshold):
    trim_end = end
    if trim_right:
        # search for nearest intron start less than
        # or equal to the current exon end
        i = bisect.bisect_right(intron_starts, end)
        if i > 0:
            nearest_intron_start = intron_starts[i-1]
            # cannot trim past start of exon and cannot
            # trim more than the overhang threshold
            if ((nearest_intron_start > start) and
                (nearest_intron_start >= end - overhang_threshold)):
                trim_end = nearest_intron_start    
    return trim_end

def trim_transcripts(transcripts, overhang_threshold):
    if overhang_threshold == 0:
        return
    # find the set of intron boundaries that govern 
    # trimming
    intron_starts, intron_ends = find_intron_starts_and_ends(transcripts)    
    for t in transcripts:
        # only the very first and last exon of each transcript can be 
        # trimmed
        trim_start = trim_left(t.exons[0].start, t.exons[0].end, 
                               intron_starts[t.strand], 
                               intron_ends[t.strand], 
                               overhang_threshold)
        trim_end = trim_right(t.exons[-1].start, t.exons[-1].end, 
                              intron_starts[t.strand], 
                              intron_ends[t.strand], 
                              overhang_threshold)
        logging.debug('strand=%d txstart=%d txend=%d trimstart=%d trimend=%d' % 
                      (t.strand, t.tx_start, t.tx_end, trim_start, trim_end))
        # if both start and end were trimmed it is
        # possible that they could be trimmed to 
        # match different introns and generate a 
        # weird and useless trimmed exon that is not
        # compatible with either the left or right
        # introns. resolve these situations by choosing
        # the smaller of the two trimmed distances as
        # the more likely
        if trim_end < trim_start:
            logging.warning('Trimming produced strand=%d txstart=%d txend=%d trimstart=%d trimend=%d' % 
                            (t.strand, t.tx_start, t.tx_end, trim_start, trim_end))
            left_trim_dist = trim_start - t.tx_start
            right_trim_dist = t.tx_end - trim_end
            assert left_trim_dist >= 0
            assert right_trim_dist >= 0
            if left_trim_dist <= right_trim_dist:
                trim_start, trim_end = trim_start, t.tx_end
            else:
                trim_start, trim_end = t.tx_start, trim_end
        assert trim_start < trim_end
        # finally, modify the transcript
        t.exons[0].start = trim_start
        t.exons[-1].end = trim_end 


def find_exon_boundaries(transcripts):
    '''
    input: a list of transcripts (not Node objects, these are transcripts)
    parsed directly from an input file and not added to an isoform graph
    yet. 
    
    output: sorted list of exon boundaries    
    '''
    exon_boundaries = set()
    # first add introns to the graph and keep track of
    # all intron boundaries
    for transcript in transcripts:
        # add transcript exon boundaries
        for exon in transcript.exons:
            # keep track of positions where introns can be joined to exons
            exon_boundaries.add(exon.start)
            exon_boundaries.add(exon.end)
    # sort the intron boundary positions and add them to interval trees
    return sorted(exon_boundaries)

def split_exon(exon, transcript_coverage, boundaries):
    # find the indexes into the intron boundaries list that
    # border the exon.  all the indexes in between these two
    # are overlapping the exon and we must use them to break
    # the exon into pieces 
    start_ind = bisect.bisect_right(boundaries, exon.start)
    end_ind = bisect.bisect_left(boundaries, exon.end)
    exon_splits = [exon.start] + boundaries[start_ind:end_ind] + [exon.end]
    for j in xrange(1, len(exon_splits)):
        start, end = exon_splits[j-1], exon_splits[j]
        score = float(end - start) * transcript_coverage
        yield start, end, score


class TranscriptGraph(object):
    def __init__(self):
        pass

    def get_exon_ids(self, n):
        exon_data_list = self.G.node[n]['data']
        return [x.id for x in exon_data_list]

    @staticmethod
    def from_transcripts(transcripts, overhang_threshold=0):
        g = TranscriptGraph()
        g.add_transcripts(transcripts, overhang_threshold)
        return g

    def _add_node(self, n, tdata):
        if n not in self.G:  
            self.G.add_node(n, data=[])        
        nd = self.G.node[n]
        nd['data'].append(tdata)

    def _add_edge(self, u, v, tdata):
        if not self.G.has_edge(u, v):
            self.G.add_edge(u, v, data=[])
        ed = self.G.edge[u][v]
        ed['data'].append(tdata)
    
    def _add_exon(self, exon, id, strand, cov, boundaries):
        nfirst, tdatafirst = None, None
        n1 = None
        n2 = None
        tdata = None
        for start, end, score in split_exon(exon, cov, boundaries):
            n2 = Exon(start, end)
            tdata = TranscriptData(id=id, strand=strand, score=score)
            self._add_node(n2, tdata)
            # add edges between split exon according to 
            # strand being assembled.
            if n1 is None:
                nfirst = n2
                tdatafirst = tdata
            else:
                if cmp_strand(strand, NEG_STRAND):
                    self._add_edge(n2, n1, tdata)
                if cmp_strand(strand, POS_STRAND):
                    self._add_edge(n1, n2, tdata)
            # continue loop
            n1 = n2
        assert n2.end == exon.end
        return nfirst, tdatafirst, n2, tdata

    def _add_transcript(self, transcript, boundaries):
        exons = transcript.exons        
        strand = transcript.strand
        cov = transcript.score / transcript.length
        # add the first exon to initialize the loop
        # (all transcripts must have at least one exon)
        e1_start_node, e1_start_tdata, e1_end_node, e1_end_tdata = \
            self._add_exon(exons[0], transcript.id, strand, cov, boundaries)
        for e2 in exons[1:]:
            # add exon
            e2_start_node, e2_start_tdata, e2_end_node, e2_end_tdata = \
                self._add_exon(e2, transcript.id, strand, cov, boundaries)
            # add intron -> exon edges
            if strand != NO_STRAND:
                if strand == NEG_STRAND:
                    self._add_edge(e2_start_node, e1_end_node, e2_start_tdata)
                else:
                    self._add_edge(e1_end_node, e2_start_node, e1_end_tdata)
            # continue loop
            e1_end_node = e2_end_node
            e1_end_tdata = e2_end_tdata

    def add_transcripts(self, transcripts, overhang_threshold=0):
        '''
        overhang_threshold: integer greater than zero specifying the 
        maximum exon overhang that can be trimmed to match an intron
        boundary.  exons that overhang more than this will be 
        considered independent transcript start sites or end sites
        
        note: this method cannot be called multiple times.  each time this
        function is invoked, the previously stored transcripts will be 
        deleting and overwritten        
        '''
        self.G = nx.DiGraph()
        # trim the transcripts (modifies transcripts in place)
        trim_transcripts(transcripts, overhang_threshold)
        # find the intron domains of the transcripts
        boundaries = find_exon_boundaries(transcripts)
        # add transcripts
        for t in transcripts:
            self._add_transcript(t, boundaries)

    def assemble(self, max_paths, fraction_major_path=0.10):
        if fraction_major_path <= 0:
            fraction_major_path = 1e-8        
        for res in assemble_transcript_graph(self.G, fraction_major_path, 
                                             max_paths):
            yield res
