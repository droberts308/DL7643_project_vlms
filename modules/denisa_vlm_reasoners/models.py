import os
import warnings

import torch
import torch.nn as nn

warnings.filterwarnings("ignore")
import pdb
import pickle

import clip
import numpy as np
import torch.nn.functional as F
from PIL import Image
from torchvision import models as tmodels

import model_utils as gv

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")



# Vision backbones and language backbones.
class Puzzle_Net(nn.Module):
    def __init__(self, args, im_backbone=None):
        super(Puzzle_Net, self).__init__()
        vocab_path = args.vocab_path
        with open(vocab_path, "rb") as f:
            self.vocab = pickle.load(f)

        self.num_opts = 5
        self.out_dim = args.feat_size
        self.h_sz = 256
        self.dummy_question = None
        self.model_name = args.model_name
        self.use_clip_text = args.use_clip_text
        self.loss_type = args.loss_type
        self.use_single_image_head = args.use_single_image_head
        self.train_backbone = args.train_backbone
        self.word_embed = args.word_embed
        self.sorted_puzzle_ids = np.sort(np.array([int(ii) for ii in args.puzzle_ids]))

        if args.loss_type == "classifier" or args.loss_type == "puzzle_tails":
            self.max_val = gv.MAX_VAL + 1
        elif args.loss_type == "regression":
            self.max_val = 1

        # image backbones.
        if args.model_name[:6] == "resnet":
            self.im_feat_size = im_backbone.fc.weight.shape[1]
            modules = list(im_backbone.children())[:-1]
            self.im_cnn = nn.Sequential(*modules)

        # TODO[DR]: add my selection of image backbones

        else:
            raise "unknown model_name %s" % (args.model_name)

        self.create_puzzle_head(args)

        # language backbones
        if self.use_clip_text:
            self.q_encoder, _ = clip.load("ViT-B/32", device=device)
            self.clip_dim = 512
            self.q_MLP = nn.Sequential(
                nn.Linear(self.clip_dim, self.h_sz),
                nn.ReLU(),
                nn.Linear(self.h_sz, self.out_dim),
            )
        else:
            if args.word_embed == "standard":
                self.q_emb = nn.Embedding(len(self.vocab), self.h_sz, max_norm=1)
                self.q_lstm = nn.LSTM(
                    self.h_sz,
                    self.h_sz,
                    num_layers=2,
                    batch_first=True,
                    bidirectional=True,
                )
            else:
                word_dim = gv.word_dim
                self.q_emb = nn.Identity()
                self.q_lstm = nn.GRU(
                    word_dim,
                    self.h_sz,
                    num_layers=1,
                    batch_first=True,
                    bidirectional=True,
                )
            self.q_MLP = nn.Linear(self.h_sz * 2, self.out_dim)

        self.o_encoder = nn.Sequential(
            nn.Embedding(len(self.vocab), self.out_dim, max_norm=1),
            nn.Linear(self.out_dim, self.out_dim),
            nn.ReLU(),
        )
        self.qv_fusion = nn.Sequential(
            nn.Linear(self.out_dim * 2, self.out_dim),
            nn.ReLU(),
            nn.Linear(self.out_dim, self.out_dim),
            nn.ReLU(),
        )
        
        self.create_puzzle_tail(args)


    def create_puzzle_head(self, args):
        if args.use_single_image_head:
            self.im_encoder = nn.Sequential(
                nn.Linear(self.im_feat_size, self.out_dim),
                nn.ReLU(),
                nn.Linear(self.out_dim, self.out_dim),
            )
        else:
            self.puzzle_ids = args.puzzle_ids
            im_encoder = [nn.Sequential(nn.Linear(self.out_dim, 1))]
            for i in range(1, gv.num_puzzles + 1):
                im_encoder.append(
                    nn.Sequential(
                        nn.Linear(self.im_feat_size, self.out_dim),
                        nn.ReLU(),
                        nn.Linear(self.out_dim, self.out_dim),
                    )
                )
            self.im_encoder = nn.ModuleList(im_encoder)

    def create_puzzle_tail(self, args):
        self.puzzle_ids = args.puzzle_ids
        ans_decoder = [
            nn.Sequential(nn.Linear(self.out_dim, 1))
        ]  # start with a dummy as we are 1-indexed wrt puzzle ids.
        if args.puzzles == "all":
            puzzles = range(1, gv.num_puzzles + 1)
        else:
            puzzles = self.puzzle_ids
        for pid in puzzles:  # self.puzzle_ids:
            num_classes = (
                gv.NUM_CLASSES_PER_PUZZLE[str(pid)]
                if args.loss_type == "classifier"
                else 1
            )
            if int(pid) not in gv.SEQ_PUZZLES:
                ans_decoder.append(
                    nn.Sequential(
                        nn.Linear(self.out_dim, self.out_dim),
                        nn.ReLU(),
                        nn.Linear(self.out_dim, self.out_dim),
                        nn.ReLU(),
                        nn.Linear(self.out_dim, num_classes),
                    )
                )
            else:
                ans_decoder.append(
                    nn.LSTM(self.out_dim, num_classes, num_layers=1, batch_first=True)
                )
        self.ans_decoder = nn.ModuleList(ans_decoder)


    def save_grad_hook(self):
        self.vis_grad = None

        def bwd_hook(module, in_grad, out_grad):
            self.vis_grad = out_grad

        return bwd_hook

    def save_fwd_hook(self):
        self.vis_conv = None

        def fwd_hook(__, _, output):
            self.vis_conv = output

        return fwd_hook

    def encode_image(self, im, pids=None):
        if self.train_backbone:
            x = self.im_cnn(im).squeeze()
        else:
            with torch.no_grad():
                x = self.im_cnn(im).squeeze()

        if len(x.shape) == 1:
            x = x.unsqueeze(0)

        if self.use_single_image_head:
            y = self.im_encoder(x)
        else:
            y = torch.zeros(len(im), self.out_dim).to(device)
            for t in range(len(self.puzzle_ids)):
                idx = pids == int(self.puzzle_ids[t])
                idx = idx.to(device)
                if idx.sum() > 0:
                    y[idx] = F.relu(self.im_encoder[int(self.puzzle_ids[t])](x[idx]))

        return y

    def decode_text(self, text):
        get_range = lambda x: range(1, x) if x < 70 else range(x - 70 + 4, x)
        tt = text.cpu()
        text = [
            " ".join(
                [
                    self.vocab.idx2word[int(j)]
                    for j in tt[i][get_range(torch.nonzero(tt[i])[-1])]
                ]
            )
            for i in range(len(tt))
        ]
        return text

    def encode_text(self, text):
        if self.word_embed == "standard":
            x = self.q_emb(text)
            x, (h, _) = self.q_lstm(x.float())
            x = F.relu(self.q_MLP(x.mean(1)))
        elif self.word_embed == "bert":
            text = self.decode_text(text)
            q_enc = torch.zeros(len(text), gv.max_qlen, gv.word_dim).to(device)
            for ii, tt in enumerate(text):
                q_feat = gv.word_embed(tt)
                q_enc[ii, : min(gv.max_qlen, len(q_feat)), :] = q_feat
            x, (h, _) = self.q_lstm(q_enc.float())
            x = F.relu(self.q_MLP(x.mean(1)))
        else:
            x = gv.word_embed(text)

        return x

    def seq_decoder(self, decoder, feat):
        """run the LSTM decoder sequentially for k steps"""
        out = [None] * gv.MAX_DECODE_STEPS
        hx = None
        for k in range(gv.MAX_DECODE_STEPS):
            try:
                out[k], hx = decoder(feat, hx)
            except:
                pdb.set_trace()
        return out

    def decode_individual_puzzles(self, feat, pids):
        upids = torch.unique(pids)
        out_feats = {}
        for t in range(len(upids)):
            idx = pids == upids[t]
            key = str(upids[t].item())
            key_idx = (
                np.where(int(key) == np.array(self.sorted_puzzle_ids))[0][0] + 1
            )  # +1 because we use 1-indexed.
            if upids[t] not in gv.SEQ_PUZZLES:
                out_feats[int(key)] = self.ans_decoder[key_idx](feat[idx])
            else:
                out_feats[int(key)] = self.seq_decoder(
                    self.ans_decoder[key_idx], feat[idx]
                )
        return out_feats

    def forward(self, im, q=None, puzzle_ids=None):
        im_feat = self.encode_image(im, puzzle_ids)
        q_feat = self.encode_text(q)
        qv_feat = self.qv_fusion(torch.cat([im_feat, q_feat], dim=1))
        
        qvo_feat = self.decode_individual_puzzles(qv_feat, puzzle_ids)
        return qvo_feat

def load_pretrained_models(args, model_name, model=None):

    if args.test and model is not None:
        model_path = os.path.join(
            args.location,
            "ckpt_%s_%s_%s.pth" % (args.model_name, args.word_embed, args.seed),
        )
        print("test: loading checkpoint %s ..." % (model_path))
        checkpoint = torch.load(model_path)
        model.load_state_dict(checkpoint["net"], strict=True)
        return

    preprocess = None
    if args.model_name in ["resnet18"]:
        model = tmodels.__dict__[args.model_name](pretrained=True)

    elif args.model_name in ["resnet50"]:  # use_resnet:
        from torchvision.models import ResNet50_Weights, resnet50

        weights = ResNet50_Weights.DEFAULT
        model = resnet50(weights=weights)
        preprocess = weights.transforms()
    else:
        print("model name is %s: not loading pre-trained model." % (args.model_name))

    return model, preprocess