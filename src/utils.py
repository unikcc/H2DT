#!/usr/bin/env python

import torch
import numpy as np
from scipy.linalg import block_diag
from collections import defaultdict
from box import Box

from torch.utils.data import Dataset, DataLoader
import os
import pickle as pkl
import random
from loguru import logger
import json
import dgl

# from src.common import WordPair, build_hgraph
from src.common import WordPair, build_hgraph
from src.preprocess import Preprocessor
# from src.run_eval1 import Template as Run_eval
from src.run_eval import Template as Run_eval

class MyDataset(Dataset):
    def __init__(self, data):
        self.data = data

    def __getitem__(self, index):
        return self.data[index]

    def __len__(self):
        return len(self.data)

class MyDataLoader:
    def __init__(self, cfg):
        path = os.path.join(cfg.preprocessed_dir, '{}_{}.pkl'.format(cfg.lang, cfg.bert_path.replace('/', '-')))
        preprocessor = Preprocessor(cfg)
        
        data = None
        if not os.path.exists(path):
            logger.info('Preprocessing data...')
            data = preprocessor.forward()
            logger.info('Saving preprocessed data to {}'.format(path))
            if not os.path.exists(cfg.preprocessed_dir):
                os.makedirs(cfg.preprocessed_dir)
            pkl.dump(data, open(path, 'wb'))
        
        logger.info('Loading preprocessed data from {}'.format(path))
        self.data = pkl.load(open(path, 'rb')) if data is None else data

        self.kernel = WordPair()
        self.config = cfg 

    def worker_init(self, worked_id):
        worker_seed = torch.initial_seed() % 2**32
        np.random.seed(worker_seed)
        random.seed(worker_seed)
    
    def collate_fn(self, lst):
        doc_id, input_ids, input_masks, input_segments,sentence_length, token2sents, utterance_index, \
            token_index, thread_length, token2speaker, reply_mask, speaker_mask, thread_mask, pieces2words, new2old, \
                triplets, pairs, entity_list, rel_list, speakers, replies = zip(*lst)

        dialogue_length = list(map(len, input_ids))

        # max_lens = max(map(lambda line: max(map(len, line)), input_ids))
        # padding = lambda input_batch: [w + [0] * (max_lens - len(w)) for line in input_batch for w in line]
        # input_ids, input_masks, input_segments = map(padding, [input_ids, input_masks, input_segments])
        max_lens = max([len(w) for w in input_ids])
        input_ids = [w + [0] * (max_lens - len(w)) for w in input_ids]
        input_masks = [w + [0] * (max_lens - len(w)) for w in input_masks]
        input_segments = [w + [0] * (max_lens - len(w)) for w in input_segments] 

        max_lens = max(map(len, token2sents))
        padding = lambda input_batch: [w + [0] * (max_lens - len(w)) for w in input_batch]
        token2sents, utterance_index, token_index, token2speaker = map(padding, [token2sents, utterance_index, token_index, token2speaker])

        padding_list = lambda input_batch : [list(map(list, w)) + [[0, 0, 0]] * (max(map(len, input_batch)) - len(w)) for w in input_batch]
        entity_lists, rel_lists = map(padding_list, [entity_list, rel_list])

        max_tri_num = max(map(len, triplets))
        triplet_masks = [[1] * len(w) + [0] * (max_tri_num - len(w)) for w in triplets]
        triplets = [list(map(list, w)) + [[0] * 7] * (max_tri_num - len(w)) for w in triplets]

        sentence_masks = np.zeros([len(token2sents), max_lens, max_lens], dtype=int)
        for i in range(len(sentence_length)):
            masks = [np.triu(np.ones([lens, lens], dtype=int)) for lens in sentence_length[i]]
            masks = block_diag(*masks)
            sentence_masks[i, :len(masks), :len(masks)] = masks
        sentence_masks = sentence_masks.tolist()

        flatten_length = list(map(sum, sentence_length))
        cur_masks = (np.expand_dims(np.arange(max(flatten_length)), 0) < np.expand_dims(flatten_length, 1)).astype(np.int64)
        full_masks = (np.expand_dims(cur_masks, 2) * np.expand_dims(cur_masks, 1)).tolist()

        entity_matrix = self.kernel.list2rel_matrix4batch(entity_lists, max_lens)
        rel_matrix = self.kernel.list2rel_matrix4batch(rel_lists, max_lens)
        # polarity_matrix = self.kernel.list2rel_matrix4batch(polarity_lists, max_lens)

        new_reply_masks = np.zeros([len(reply_mask), max_lens, max_lens])
        for i in range(len(new_reply_masks)):
            lens = len(reply_mask[i])
            new_reply_masks[i, :lens, :lens] = reply_mask[i]

        new_speaker_masks = np.zeros([len(speaker_mask), max_lens, max_lens])
        for i in range(len(new_speaker_masks)):
            lens = len(speaker_mask[i])
            new_speaker_masks[i, :lens, :lens] = speaker_mask[i]

        new_thread_masks = np.zeros([len(thread_mask), max_lens, max_lens])
        for i in range(len(new_thread_masks)):
            lens = len(thread_mask[i])
            new_thread_masks[i, :lens, :lens] = thread_mask[i]
        
        gs = []
        for i in range(len(speakers)):
            g = build_hgraph(token2sents[i], speakers[i], replies[i], dialogue_length[i])
            gs.append(g)
        hgraphs = dgl.batch(gs).to(self.config['device'])

        res = {
            "doc_id": doc_id,
			"input_ids": input_ids, "input_masks": input_masks, "input_segments": input_segments,
            'ent_matrix': entity_matrix,   'rel_matrix': rel_matrix,
            'sentence_masks': sentence_masks, 'full_masks': full_masks,             
            'triplets': triplets, 'triplet_masks': triplet_masks, 'pairs': pairs,
            'token2sents': token2sents, 'dialogue_length': dialogue_length,
            'utterance_index': utterance_index, 'token_index': token_index,
            'thread_lengths': thread_length, 'token2speakers': token2speaker,
            'reply_masks': new_reply_masks, 'speaker_masks': new_speaker_masks, 'thread_masks': new_thread_masks,
            'pieces2words': pieces2words, 'new2old': new2old,
            'hgraphs': hgraphs
        }

        nocuda = ['thread_lengths', 'pairs', 'doc_id', 'pieces2words', 'new2old', 'hgraphs']
        res = {k: v if k in nocuda else torch.tensor(v).to(self.config.device) for k, v in res.items()}
        return res
       
    def getdata(self):
        
        load_data = lambda mode: DataLoader(MyDataset(self.data[mode]), num_workers=0, worker_init_fn=self.worker_init, 
                                                shuffle=(mode == 'train'),  batch_size=self.config.batch_size, collate_fn=self.collate_fn)
        
        train_loader, valid_loader, test_loader = map(load_data, 'train valid test'.split())

        line = 'polarity_dict target_dict aspect_dict opinion_dict entity_dict relation_dict'.split()
        for w, z in zip(line, self.data['label_dict']):
            self.config[w] = z

        res = (train_loader, valid_loader, test_loader, self.config)

        return res
    
class RelationMetric:
    def __init__(self, config):
        self.clear()
        self.kernel = WordPair()
        self.predict_result = defaultdict(list)
        self.config = config
    
    def trans2position(self, triplet, new2old, pieces2words):
        res = []
        """
        recover the position of entities in the original sentence

        new2old: transfer position from index with CLS and SEP to index without CLS and SEP
        pieces2words: transfer position from index of wordpiece to index of original words 

        Example:
        list0 (original sentence):"London is the capital of England"
        list1 (tokenized sentence): "Lon ##don is the capital of England"
        list2 (packed sentence): "[CLS] Lon #don is the capital of England [SEP]"
        predicted entity: (1, 2), denotes "Lon #don" in list2

        new2old: list2->list1
          = {'1': 0, '2': 1, '3': 2, '4': 3, '5': 4, ...}
        pieces2words: list1->list0
          = {'0': 0, '1': 0, '2': 1, '3': 2, '4': 3, ...}

        input  -> entity in list2: "Lon #don" (1, 2)
        middle -> entity in list1: "Lon #don" (0, 1)
        output -> entity in list0: "London"   (0, 0)
        """

        head = lambda x : pieces2words[new2old[x]]
        tail = lambda x : pieces2words[new2old[x]]

        triplet = list(triplet)
        for s0, e0, s1, e1, s2, e2, pol in triplet:
            ns0, ns1, ns2 = head(s0), head(s1), head(s2)
            ne0, ne1, ne2 = tail(e0), tail(e1), tail(e2)
            res.append([ns0, ne0, ns1, ne1, ns2, ne2, pol])
        return res
    
    def trans2pair(self, pred_pairs, new2old, pieces2words):
        new_pairs = {}
        new_pos = lambda x : pieces2words[new2old[x]]
        for k, line in pred_pairs.items():
            new_line = []
            for s0, e0, s1, e1 in line:
                s0, e0, s1, e1 = new_pos(s0), new_pos(e0), new_pos(s1), new_pos(e1)
                new_line.append([s0, e0, s1, e1])
            new_pairs[k] = new_line
        return new_pairs

    def filter_entity(self, ent_list, new2old, pieces2words):
        res = []

        # If the entity is a sub-string of another entity, remove it
        # ent_list = sorted(ent_list, key=lambda x: (x[0], -x[1]))
        # ent_list = [w for i, w in enumerate(ent_list) if i == 0 or w[0] != ent_list[i-1][0]]

        for s, e, pol in ent_list:
            ns, ne = pieces2words[new2old[s]], pieces2words[new2old[e]]
            res.append([ns, ne, pol])
        return res

    def add_instance(self, data, pred_ent_matrix, pred_rel_matrix):
        """
        input_matrix: [B, Seq, Seq]
        pred_matrix: [B, Seq, Seq, 6]
        input_masks: [B, Seq]
        """
        pol_scores = np.array((pred_rel_matrix.softmax(-1) * data['full_masks'].unsqueeze(-1)).tolist())

        pred_ent_matrix = pred_ent_matrix.argmax(-1) * data['sentence_masks']
        pred_rel_matrix = pred_rel_matrix.argmax(-1) * data['full_masks']


        token2sents = data['token2sents'].tolist()
        new2old = data['new2old']
        pieces2words = data['pieces2words']
        doc_id = data['doc_id']

        pred_rel_matrix = np.array(pred_rel_matrix.tolist())
        pred_ent_matrix = np.array(pred_ent_matrix.tolist())

        for i in range(len(pred_ent_matrix)):
            ent_matrix, rel_matrix = pred_ent_matrix[i], pred_rel_matrix[i]
            pol_score = pol_scores[i]
            pred_triplet, pred_pairs = self.kernel.get_triplets(ent_matrix, rel_matrix, token2sents[i], pol_score)
            pred_ents = self.kernel.rel_matrix2list(ent_matrix)

            pred_ents = self.filter_entity(pred_ents, new2old[i], pieces2words[i])
            pred_pairs = self.trans2pair(pred_pairs, new2old[i], pieces2words[i])
            pred_triplet = self.trans2position(pred_triplet, new2old[i], pieces2words[i])

            self.predict_result[doc_id[i]].append(pred_ents)
            self.predict_result[doc_id[i]].append(pred_pairs)
            self.predict_result[doc_id[i]].append(pred_triplet)

    def clear(self):
        self.predict_result = defaultdict(list)

    def save2file(self, gold_file, pred_file):
        # pol_dict = {"O": 0, "pos": 1, "neg": 2, "other": 3}
        # pol_dict = self.config.polarity_dict
        pol_dict = {"O": 0, 'h2h': 1, "pos": 2, "neg": 3, "other": 4}
        reverse_pol_dict = {v: k for k, v in pol_dict.items()}
        reverse_ent_dict = {v: k for k, v in self.config.entity_dict.items()}

        with open(gold_file, 'r', encoding='utf-8') as f:
            data = json.load(f)

        res = []
        for line in data:
            doc_id, sentence = line['doc_id'], line['sentences']
            if doc_id not in self.predict_result:
                continue
            doc = ' '.join(sentence).split()
            new_triples = []

            prediction = self.predict_result[doc_id]
            entities = defaultdict(list)
            for head, tail, tp in prediction[0]:
                tp = reverse_ent_dict[tp]
                head, tail = head, tail + 1
                tp_dict = {'ENT-T': 'targets', 'ENT-A': 'aspects', 'ENT-O': 'opinions'}
                entities[tp_dict[tp]].append([head, tail])

            pairs = defaultdict(list)
            for key in ['ta', 'to', 'ao']:
                for s0, e0, s1, e1 in prediction[1][key]:
                    e0, e1 = e0 + 1, e1 + 1
                    pairs[key].append([s0, e0, s1, e1])

            new_triples = []
            for s0, e0, s1, e1, s2, e2, pol in prediction[2]:
                pol = reverse_pol_dict[pol]
                e0, e1, e2 = e0 + 1, e1 + 1, e2 + 1
                new_triples.append([s0, e0, s1, e1, s2, e2, pol, ' '.join(doc[s0:e0]), ' '.join(doc[s1:e1]), ' '.join(doc[s2:e2])])

            res.append({'doc_id': doc_id, 'triplets': new_triples, \
                        'targets': entities['targets'], 'aspects': entities['aspects'], 'opinions': entities['opinions'],\
                        'ta': pairs['ta'], 'to': pairs['to'], 'ao': pairs['ao']})
        logger.info('Save prediction results to {}'.format(pred_file))
        json.dump(res, open(pred_file, 'w', encoding='utf-8'), ensure_ascii=False)
    
    def compute(self, name='valid'):
        # action: pred, make prediction, save to file 
        # action: eval, make prediction, save to file and evaluate 

        args = Box({
            'pred_file': os.path.join(self.config.target_dir, 'pred_{}_{}.json'.format(self.config.lang, name)),
            'gold_file': os.path.join(self.config.json_path, '{}.json'.format(name))
            # 'gold_file': os.path.join(self.config.json_path, '{}_gold.json'.format(name))
        })
        self.save2file(args.gold_file, args.pred_file)

        micro, iden, res, intra, inter = Run_eval(args).forward()
        self.clear()
        return micro[2], iden[2], res, intra[2], inter[2]