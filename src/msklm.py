from .fastai_data import *
from .encode_data import *
from .music_transformer import *
from fastai.basics import *
from fastai.text.models.transformer import _line_shift, init_transformer
from fastai.text.models.awd_lstm import *
from fastai.text.models.transformer import *

from .encode_data import VALTSEP, SAMPLE_FREQ

# DATALOADING AND TRANSFORMATIONS

TaskType = Enum('TaskType', 'MaskOnly, NextWord, Seq2Seq, NextSent')

# MLM Transform
def msklm_mask(shape, p=0.2, tile=1):
    p = p / tile
    rand_mask = torch.rand(*shape) < p
    if tile > 1:
        rand_mask = torch.repeat_interleave(rand_mask, tile, dim=1)[:rand_mask.shape[0], :rand_mask.shape[1]]
        
    lm_mask = torch.roll(rand_mask, 1, dims=1)
    lm_mask[:, 0] = 0
    lm_mask = rand_mask & lm_mask
    return rand_mask, lm_mask

def msklm_tfm(b, mask_idx=vocab.mask_idx, pad_idx=vocab.pad_idx, p=0.2, tile=1):
    x,y = b
    
    rand_mask, lm_mask = msklm_mask(y.shape, p, tile)
    
    x_msk = y.clone()
    x_msk[rand_mask] = mask_idx

    y_msk = y.clone()
    y_msk[~rand_mask] = pad_idx
    
    x_lm = torch.zeros_like(x)
    x_lm[lm_mask] = x[lm_mask]
    
    return (x_msk, x_lm), y_msk

# Utility for predictions
def mask_input(xb, mask_range=vocab.note_range, mask_idx=vocab.mask_idx):
    xb = xb.clone()
    xb[(xb >= mask_range[0]) & (xb < mask_range[1])] = mask_idx
    return xb

# Sequence 2 Sequence Translate

class S2SFileProcessor(PreProcessor):
    "`PreProcessor` that opens the filenames and read the texts."
    def process_one(self,item):
        out = np.load(item, allow_pickle=True)
        if out.shape != (2,): return None
        if len(out[0]) > 2048: return None
        if len(out[1]) > 2048: return None
        if len(out[0]) < 16: return None
        if len(out[1]) < 16: return None
#         return np.array([out[0].reshape(-1), out[1].reshape(-1)])
        return out
    
    def process(self, ds:Collection):
        ds.items = [self.process_one(item) for item in ds.items]
        ds.items = [i for i in ds.items if i is not None]
#         ds.items = array([self.process_one(item) for item in ds.items], dtype=np.object)

def avg_tempo(t, sep_idx=VALTSEP):
    avg = t[t[:, 0] == sep_idx][:, 1].sum()/t.shape[0]
    avg = int(round(avg/SAMPLE_FREQ))
    return 'mt'+str(min(avg, MTEMPO_SIZE-1))

def avg_pitch(t, sep_idx=VALTSEP):
    return t[t[:, 0] > sep_idx][:, 0].mean()

class S2SPreloader(Callback):
    def __init__(self, dataset:LabelList, bptt:int=512, y_offset=1, transpose_range=(0,12), **kwargs):
        # y_offset = extra padding for translation
        self.dataset,self.bptt = dataset,bptt
        self.np = vocab
        self.y_offset = y_offset
        self.single_tfm = partial(to_single_stream, vocab=vocab)
        self.transpose_tfm = partial(rand_transpose_tfm, note_range=vocab.note_range, rand_range=transpose_range)
    
    def __getitem__(self, k:int):
        item,_ = self.dataset[k]
        x,y = item
        if random.randint(0,1) == 1: x,y = y,x # switch translation order around
        part_order = [MSEQ, CSEQ] if avg_pitch(x) > avg_pitch(y) else [CSEQ, MSEQ] # Assuming melody has higher pitch
        x = partenc2seq2seq(x, part_type=part_order[0], bptt=self.bptt)
        y = partenc2seq2seq(y, part_type=part_order[1], bptt=self.bptt+1, translate=True) # offset bptt for decoder shift
        x,y = self.transpose_tfm([x,y])
        return x, y
    
    def __len__(self):
        return len(self.dataset)
    
def partenc2seq2seq(part_np, part_type=MSEQ, vocab=vocab, bptt=512, translate=False):
    part_meta = np.array([vocab.stoi[part_type], vocab.pad_idx])
#     part_meta = np.array([vocab.stoi[part_type], vocab.stoi[avg_tempo(part_np)]])
    s2s_out = to_single_stream(part_np, start_seq=part_meta)
    
    pad_first = 1 if translate else 0
    s2s_out = np.pad(s2s_out, (pad_first,1), 'constant', constant_values=(vocab.stoi[S2SCLS], vocab.stoi[EOS]))
    
    s2s_out = np.pad(s2s_out, (0,max(bptt-s2s_out.shape[0],0)), 'constant', constant_values=vocab.pad_idx)[:bptt]
    return s2s_out

def s2s_file2parts(file, pred_melody=False):
    melody_np, chord_np = np.load(file, allow_pickle=True)

    melody_np, chord_np = (melody_np, chord_np) if avg_pitch(melody_np) > avg_pitch(chord_np) else (chord_np, melody_np) # Assuming melody has higher pitch
    mpart = partenc2seq2seq(melody_np, part_type=MSEQ, translate=pred_melody)
    cpart = partenc2seq2seq(chord_np, part_type=CSEQ, translate=not pred_melody)
    return mpart, cpart

def combined_npenc2chordarr(np1, np2):
    if len(np1.shape) == 1: np1 = to_double_stream(np1)
    if len(np2.shape) == 1: np1 = to_double_stream(np2)
    p1 = npenc2chordarr(np1)
    p2 = npenc2chordarr(np2)
    max_ts = max(p1.shape[0], p2.shape[0])
    p1w = ((0,max_ts-p1.shape[0]),(0,0),(0,0))
    p1_pad = np.pad(p1, p1w, 'constant')
    p2w = ((0,max_ts-p2.shape[0]),(0,0),(0,0))
    p2_pad = np.pad(p2, p2w, 'constant')
    chordarr_comb = np.concatenate((p1_pad, p2_pad), axis=1)
    return chordarr_comb

# preloader itself contains all the transforms
def s2s_tfm(b):
    x,y_s2s = b
    return (x,y_s2s[:,:-1]),y_s2s[:,1:]


# DataLoading
class CombinedDS(Callback):
    def __init__(self, dss):
        self.dss = self.dss
    def __getattr__(self, attr):
        def redirected(self, *args, **kwargs):
            for ds in self.dss:
                if hasattr(ds, attr):
                    getattr(ds, attr)(*args, **kwargs)
        return redirected

class CombinedDL():
    def __init__(self, dls, num_it=100):
        self.dls = dls
        self.dataset = CombinedDS([dl.dataset for dl in dls if hasattr(dl, 'dataset')])
        self.num_it = num_it
        self.dl_idx = -1
        
    def __len__(self)->int: return sum([len(dl) for dl in self.dls])
        
    def __iter__(self):
        "Process and returns items from `DataLoader`."
        iters = [iter(dl) for dl in self.dls]
        self.dl_idx = -1
        while len(iters):
            self.dl_idx = (self.dl_idx+1) % len(iters)
            for b in range(self.num_it):
                try:
                    yield next(iters[self.dl_idx])
                except StopIteration as e:
                    iters.remove(iters[self.dl_idx])
                    break
#         raise StopIteration

class CombinedData():
    def __init__(self, dbs, num_it=100):
        self.dbs = dbs
        self.train_dl = CombinedDL([db.train_dl for db in self.dbs], num_it)
        self.valid_dl = CombinedDL([db.valid_dl for db in self.dbs], num_it)
        self.vocab = vocab
        self.train_ds = None
        self.path = dbs[0].path
        self.device = dbs[0].device
        self.empty_val = False

    def add_tfm(self,tfm:Callable)->None:
        for dl in self.dbs: dl.add_tfm(tfm)

    def remove_tfm(self,tfm:Callable)->None:
        for dl in self.dbs: dl.remove_tfm(tfm)
        
class MLMLearner(MusicLearner):
    
    def predict_nw(self, xb:Tensor, n_words:int=128,
                     temperatures:float=(1.0,1.0), min_bars=4,
                     top_k=40, top_p=0.9):
        "Return the `n_words` that come after `text`."
        self.model.reset()
        if xb.shape[0] > 1: xb = xb[0][None]
        seed = xb.cpu().numpy().squeeze()
        yb = torch.ones_like(xb)
        new_idx = []

        sep_count = 0

        bar_len = SAMPLE_FREQ * 4 # assuming 4/4 time
        vocab = self.data.vocab

        with torch.no_grad():
            for i in progress_bar(range(n_words), leave=True):
                task_type = torch.full_like(xb, TaskType.NextWord.value)

                res = self.pred_batch(batch=((xb,task_type,xb),yb))[-1][0, -1]

                # bar = 16 beats
                if (sep_count // 16) <= min_bars: res[vocab.bos_idx] = 0.

                # Use first temperatures value if last prediction was duration
                temperature = temperatures[0] if (len(new_idx)==0 or self.data.vocab.is_duration(new_idx[-1])) else temperatures[1]
                if temperature != 1.: res.pow_(1 / temperature)

                res = top_k_top_p_filtering(res, top_k=top_k, top_p=top_p, filter_value=0)
                idx = torch.multinomial(res, 1).item()

                if new_idx and new_idx[-1]==vocab.sep_idx: 
                    duration = idx - vocab.dur_range[0]
                    sep_count += duration
                    # print('Bars', duration, sep_count // 16)

                if idx==vocab.bos_idx: 
                    print('Predicted BOS token. Returning prediction...')
                    break


                new_idx.append(idx)
                xb = xb.new_tensor([idx])[None]
        return np.array(new_idx), seed
    
    def predict_mask(self, xb:Tensor,
                    temperatures:float=(1.0,1.0),
                    top_k=20, top_p=0.8):
        if xb.shape[0] > 1: xb = xb[0][None]
        xb = xb.clone()
        self.model.reset()
        self.model.update_mem_len(TaskType.NextSent.value)

        mask_idxs = (xb == vocab.mask_idx).nonzero()
        for midx in progress_bar(mask_idxs, leave=True):
            task_type = torch.full_like(xb, TaskType.NextSent.value)

            # Next Word
            res = self.pred_batch(batch=((xb,task_type),xb))[0]
            res = res[tuple(midx)] # task1, task2 - (bs x ts x vocab)
            
            # Don't allow any special tokens (as we are only removing notes and durations)
            res[vocab.bos_idx] = 0.
            res[vocab.sep_idx] = 0.
            
            # Use first temperatures value if last prediction was duration
            temperature = temperatures[0]
            if temperature != 1.: res.pow_(1 / temperature)

            res = top_k_top_p_filtering(res, top_k=top_k, top_p=top_p, filter_value=0)
            idx = torch.multinomial(res, 1).item()
            #         idx = res.argmax()

            xb[tuple(midx)] = idx

        return xb.cpu().numpy()

    def predict_s2s(self, xb:Tensor, yb:Tensor, n_words:int=128,
                    temperatures:float=(1.0,1.0),
                    top_k=40, top_p=0.9):
        if xb.shape[0] > 1: xb = xb[0][None]
        yb_seed = yb
        self.model.reset()
        self.model.update_mem_len(TaskType.Seq2Seq.value)

        for i in progress_bar(range(n_words), leave=True):
            task_type = torch.full_like(xb, TaskType.Seq2Seq.value)
            pad = xb.shape[-1]-yb_seed.shape[-1]
            yb_inp = F.pad(yb_seed, (0,pad), value=vocab.pad_idx)

            # Next Word
            pred_idx = yb_seed.shape[-1]-1
            res = self.pred_batch(batch=((xb,task_type,yb_inp),yb_inp))[-1][0, pred_idx] # task1, task2 - (bs x ts x vocab)

            # Encoder only - nw
    #         res = self.pred_batch(batch=((xb,task_type,xb),xb))[0][0, -1] # task1, task2 - (bs x ts x vocab)

            # Use first temperatures value if last prediction was duration
            temperature = temperatures[0] if (len(yb_seed)==0 or self.data.vocab.is_duration(yb_seed[0, -1])) else temperatures[1]
            if temperature != 1.: res.pow_(1 / temperature)

            res = top_k_top_p_filtering(res, top_k=top_k, top_p=top_p, filter_value=0)
            idx = torch.multinomial(res, 1).item()
            #         idx = res.argmax()

            if idx == vocab.bos_idx | idx == vocab.stoi[EOS]: 
                print('Predicting BOS/EOS')
                break

            t_idx = torch.tensor(idx, device=xb.device).view(1, 1)
            yb_seed = torch.cat((yb_seed, t_idx), dim=-1)

        return yb_seed.cpu().numpy()

# High level serve api
def part_enc(chordarr, part):
    partarr = chordarr[:,part:part+1,:]
    npenc = chordarr2npenc(partarr)
    return npenc
    
def s2s_predict_from_midi(learn, midi=None, n_words=200, 
                      temperatures=(1.0,1.0), top_k=24, top_p=0.7, pred_melody=True, **kwargs):

    stream = file2stream(midi) # 1.
    chordarr = stream2chordarr(stream) # 2.
    _,num_parts,_ = chordarr.shape
    melody_np, chord_np = [part_enc(chordarr, i) for i in range(num_parts)]
    

    melody_np, chord_np = (melody_np, chord_np) if avg_pitch(melody_np) > avg_pitch(chord_np) else (chord_np, melody_np) # Assuming melody has higher pitch
    
    offset = 3
    original_shape = melody_np.shape[0] * 2 if pred_melody else chord_np.shape[0] * 2 
    bptt = original_shape + n_words + offset
    bptt = max(bptt, melody_np.shape[0] * 2, chord_np.shape[0] * 2 )
    mpart = partenc2seq2seq(melody_np, part_type=MSEQ, translate=pred_melody, bptt=bptt)
    cpart = partenc2seq2seq(chord_np, part_type=CSEQ, translate=not pred_melody, bptt=bptt)
    if pred_melody:
        xb = torch.tensor(cpart)[None]
        yb = torch.tensor(mpart)[None][:, :original_shape+offset]
    else:
        xb = torch.tensor(mpart)[None]
        yb = torch.tensor(cpart)[None][:, :original_shape+offset]


    if torch.cuda.is_available(): xb, yb = xb.cuda(), yb.cuda()
    
    pred = learn.predict_s2s(xb, yb, n_words=n_words, temperatures=temperatures, top_k=top_k, top_p=top_p)
    # pred = yb

    seed_npenc = to_double_stream(xb.cpu().numpy()) # chord
    yb_npenc = to_double_stream(pred) # melody
    npenc_order = [yb_npenc, seed_npenc] if pred_melody else [seed_npenc, yb_npenc]
    chordarr_comb = combined_npenc2chordarr(*npenc_order)

    return chordarr_comb

def nw_predict_from_midi(learn, midi=None, n_words=600, 
                      temperatures=(1.0,1.0), top_k=24, top_p=0.7, **kwargs):
    seed_np = midi2npenc(midi) # music21 can handle bytes directly
    xb = torch.tensor(to_single_stream(seed_np))[None]
    if torch.cuda.is_available(): xb = xb.cuda()
    pred, seed = learn.predict_nw(xb, n_words=n_words, temperatures=temperatures, top_k=top_k, top_p=top_p)
    seed = to_double_stream(seed)
    pred = to_double_stream(pred)
    full = np.concatenate((seed,pred), axis=0)
    return full


def mask_predict_from_midi(learn, midi=None,
                           temperatures=(1.0,1.0), top_k=20, top_p=0.8, 
                           predict_notes=True,
                           **kwargs):
    seed_np = midi2npenc(midi) # music21 can handle bytes directly
    xb = torch.tensor(to_single_stream(seed_np))[None]
    mask_range = vocab.note_range if predict_notes else vocab.dur_range
    xb = mask_input(xb, mask_range=mask_range)
    if torch.cuda.is_available(): xb = xb.cuda()
    pred = learn.predict_mask(xb, temperatures=temperatures, top_k=top_k, top_p=top_p)
    pred = to_double_stream(pred)
    return pred
        
# MODEL LOADING


class MLMTrainer(LearnerCallback):
    "`Callback` that regroups lr adjustment to seq_len, AR and TAR."
    def __init__(self, learn:Learner, dataloaders=None, starting_mask_window=1):
        super().__init__(learn)
        self.dataloaders = dataloaders
        self.count = 1
        self.mw_start=starting_mask_window

    def on_epoch_begin(self, **kwargs):
        "Reset the hidden state of the model."
        model = get_model(self.learn.model)
        model.reset()
#         model.encoder.mask_size = max(self.count+self.mw_start, 100)
        
    def on_epoch_end(self, last_metrics, **kwargs):
        "Finish the computation and sends the result to the Recorder."
        # data switching happens on end because dataloader is set before epoch begin happends
        if self.dataloaders is not None: self.learn.data = self.dataloaders[self.count % len(self.dataloaders)]
        self.count += 1

def get_mlm_model(vocab_sz:int, config:dict=None, drop_mult:float=1.):
    "Create a language model from `arch` and its `config`, maybe `pretrained`."
    for k in config.keys(): 
        if k.endswith('_p'): config[k] *= drop_mult
#     tie_weights,output_p,out_bias = map(config.pop, ['tie_weights', 'output_p', 'out_bias'])
    tie_weights,output_p,out_bias = map(config.get, ['tie_weights', 'output_p', 'out_bias'])
    n_hid = config['d_model']
    embed = TransformerEmbedding(vocab_sz, n_hid, embed_p=config['embed_p'], mem_len=config['mem_len'])
    encoder = MLMEncoder(embed, n_hid, **config)
    decoder = MLMLinearDecoder(n_hid, vocab_sz, tie_encoder=embed.embed, **config)
    model = MLMHead(encoder, decoder, mem_len=config['mem_len'])
    return model.apply(init_transformer)


def mlm_model_learner(data:DataBunch, config:dict=None, drop_mult:float=1., pretrained:bool=False,
                        pretrained_fnames:OptStrTuple=None, **learn_kwargs) -> 'LanguageLearner':
    "Create a `Learner` with a language model from `data` and `arch`."
    model = get_mlm_model(config['vocab_size'], config=config, drop_mult=drop_mult)
#     learn = UnilmLearner(data, model, config=config, split_func=tfmerXL_lm_split,
#     learn = UnilmLearner(data, model, config=config, split_func=None,
#                         **learn_kwargs)
    learn = MusicLearner(data, model, config=config, split_func=None,
                        **learn_kwargs)
    return learn

# Attn

class MemMultiHeadRelativeAttentionKV(nn.Module):
    "MutiHeadAttention with relative positional encoding."
    def __init__(self, n_heads:int, d_model:int, d_head:int=None, resid_p:float=0., attn_p:float=0., bias:bool=True,
                 scale:bool=True, mem_len:int=512, r_mask=True):
        super().__init__()
        d_head = ifnone(d_head, d_model//n_heads)
        self.n_heads,self.d_head,self.scale = n_heads,d_head,scale
        
        assert(d_model == d_head * n_heads)
#         self.out = nn.Linear(n_heads * d_head, d_model, bias=bias)
        self.q_wgt = nn.Linear(d_model, n_heads * d_head, bias=bias)
        self.k_wgt = nn.Linear(d_model, n_heads * d_head, bias=bias)
        self.v_wgt = nn.Linear(d_model, n_heads * d_head, bias=bias)
        
        self.drop_att,self.drop_res = nn.Dropout(attn_p),nn.Dropout(resid_p)
        self.ln = nn.LayerNorm(d_model)
        self.r_attn = nn.Linear(d_model, n_heads * d_head, bias=bias)
        self.r_mask = r_mask

        self.mem_len = mem_len
        self.prev_k = None
        self.prev_v = None
        
    def forward(self, q:Tensor, k:Tensor=None, v:Tensor=None, 
                r:Tensor=None, g_u:Tensor=None, g_v:Tensor=None, 
                mask:Tensor=None, **kwargs):
        if k is None: k = q
        if v is None: v = q
#         return self.ln(q + self.drop_res(self.out(self._apply_attention(q, k, v, r, g_u, g_v, mask=mask, **kwargs))))
        return self.ln(q + self.drop_res(self._apply_attention(q, k, v, r, g_u, g_v, mask=mask, **kwargs)))

    def mem_k(self, k):
        if self.mem_len == 0: return k
        if self.prev_k is None or (self.prev_k.shape[0] != k.shape[0]): # reset if wrong batch size
            self.prev_k = k
            return k
        with torch.no_grad():
            k_ext = torch.cat([self.prev_k, k], dim=1)
            self.prev_k = k_ext[:, -self.mem_len:]
        return k_ext.detach()

    def mem_v(self, v):
        if self.mem_len == 0: return v
        if self.prev_v is None or (self.prev_v.shape[0] != v.shape[0]): # reset if wrong batch size
            self.prev_v = v
            return v
        with torch.no_grad():
            v_ext = torch.cat([self.prev_v, v], dim=1)
            self.prev_v = v_ext[:, -self.mem_len:]
        return v_ext.detach()
        
    def reset(self):
        self.prev_v = None
        self.prev_k = None
        
    def _apply_attention(self, q:Tensor, k:Tensor, v:Tensor, 
                         r:Tensor=None, g_u:Tensor=None, g_v:Tensor=None, 
                         mask:Tensor=None, **kwargs):
        #Notations from the paper: x input, r vector of relative distance between two elements, u et v learnable
        #parameters of the model common between all layers, mask to avoid cheating and mem the previous hidden states.
#         bs,x_len,seq_len = q.size(0),q.size(1),r.size(0)
        k = self.mem_k(k)
        v = self.mem_v(v)
        bs,x_len,seq_len = q.size(0),q.size(1),k.size(1)
        wq,wk,wv = self.q_wgt(q),self.k_wgt(k),self.v_wgt(v)
        wq = wq[:,-x_len:]
        wq,wk,wv = map(lambda x:x.view(bs, x.size(1), self.n_heads, self.d_head), (wq,wk,wv))
        wq,wk,wv = wq.permute(0, 2, 1, 3),wk.permute(0, 2, 3, 1),wv.permute(0, 2, 1, 3)
        wkr = self.r_attn(r[-seq_len:])
        wkr = wkr.view(seq_len, self.n_heads, self.d_head)
        wkr = wkr.permute(1,2,0)
        #### compute attention score (AC is (a) + (c) and BS is (b) + (d) in the paper)
        AC = torch.matmul(wq+g_u,wk)
        BD = _line_shift(torch.matmul(wq+g_v, wkr), mask=self.r_mask)
        if self.scale: attn_score = (AC + BD).mul_(1/(self.d_head ** 0.5))
        if mask is not None: 
            mask = mask[...,-seq_len:]
            attn_score = attn_score.float().masked_fill(mask, -float('inf')).type_as(attn_score)
        attn_prob = self.drop_att(F.softmax(attn_score, dim=-1))
        attn_vec = torch.matmul(attn_prob, wv)
        return attn_vec.permute(0, 2, 1, 3).contiguous().view(bs, x_len, -1)
    
    
class MLMHead(nn.Module):
    def __init__(self, encoder, decoder, mem_len):
        super().__init__()
        self.encoder = encoder
        self.decoder = decoder
        self.default_mem_len = mem_len
        self.current_mem_len = None
    
    def forward(self, x_msk, x_lm):
#         self.update_mem_len(task_value)
#         self.encoder.mask = task_value == TaskType.NextWord.value # mask encoder for next word (so decoder can't cheat)
        x_enc = self.encoder(x_msk, x_lm)
        dec = self.decoder(x_enc) # all tasks include mask decoding
        return dec
    
    "A sequential module that passes the reset call to its children."
    def reset(self):
        for module in self.children(): 
            reset_children(module)
            
    def update_mem_len(self, task_value):
        # Only Next word predictions should have memory
        next_mem_len = self.default_mem_len if task_value == TaskType.NextWord.value else 0
        if self.current_mem_len == next_mem_len: return
        # print('Updating mem length to:', next_mem_len)
        for module in self.children(): 
            update_mem_len(module, next_mem_len)
        self.current_mem_len = next_mem_len
        self.reset()
        
def reset_children(mod):
    if hasattr(mod, 'reset'): mod.reset()
    for module in mod.children(): 
        reset_children(module)
        
def update_mem_len(mod, mem_len):
    if hasattr(mod, 'mem_len'): mod.mem_len = mem_len
    for module in mod.children(): 
        update_mem_len(module, mem_len)
        
# COMPONENTS
class TransformerEmbedding(nn.Module):
    "Embedding + positional encoding + dropout"
    def __init__(self, vocab_sz:int, emb_sz:int, embed_p:float=0., mem_len=512):
        super().__init__()
        self.emb_sz = emb_sz
        
        self.embed = nn.Embedding(vocab_sz, emb_sz, padding_idx=vocab.pad_idx)
        # See https://arxiv.org/abs/1711.09160
        with torch.no_grad(): trunc_normal_(self.embed.weight, std=0.01)
#         self.embed = embedding(vocab_sz, emb_sz)
        self.pos_enc = PositionalEncoding(emb_sz)
        self.drop = nn.Dropout(embed_p)
        self.mem_len = mem_len
    
    def forward(self, inp, pos_forward=False):
        emb = self.drop(self.embed(inp))
        x_len = inp.shape[-1]
        seq_len = x_len + self.mem_len
        if pos_forward:
            pos = torch.arange(0, seq_len, device=inp.device, dtype=emb.dtype) # forwards
        else:
            pos = torch.arange(seq_len-1, -1, -1, device=inp.device, dtype=emb.dtype) # backwards (txl pos encoding)
        return emb, self.pos_enc(pos)

class MLMLinearDecoder(nn.Module):
    "To go on top of a RNNCore module and create a Language Model."
    initrange=0.1

    def __init__(self, n_hid:int, n_out:int, output_p:float, tie_encoder:nn.Module=None, out_bias:bool=True, **kwargs):
        super().__init__()
        self.decoder = nn.Linear(n_hid, n_out, bias=out_bias)
        self.decoder.weight.data.uniform_(-self.initrange, self.initrange)
        self.output_dp = RNNDropout(output_p)
        if out_bias: self.decoder.bias.data.zero_()
        if tie_encoder: self.decoder.weight = tie_encoder.weight

    def forward(self, input:Tuple[Tensor,Tensor])->Tuple[Tensor,Tensor,Tensor]:
        output = self.output_dp(input)
        decoded = self.decoder(output)
        return decoded


# DECODER TRANSLATE BLOCK
class MLMEncoder(nn.Module):
    def __init__(self, embed:nn.Module, n_hid:int, n_layers:int, n_heads:int, d_model:int, d_head:int, d_inner:int, 
                 resid_p:float=0., attn_p:float=0., ff_p:float=0., bias:bool=True, scale:bool=True,
                 act:Activation=Activation.ReLU, double_drop:bool=True, attn_cls:Callable=MemMultiHeadRelativeAttentionKV,
                 learned_pos_enc:bool=False, mask:bool=True, mem_len:int=512, **kwargs):
        super().__init__()
        self.embed = embed
        self.u = nn.Parameter(torch.Tensor(n_heads, 1, d_head)) #Remove 1 for einsum implementation of attention
        self.v = nn.Parameter(torch.Tensor(n_heads, 1, d_head)) #Remove 1 for einsum implementation of attention
        self.n_layers,self.d_model,self.mask = n_layers,d_model,mask
        self.layers = nn.ModuleList([MLMEncoderBlock(n_heads, d_model, d_head, d_inner, resid_p=resid_p, attn_p=attn_p,
                      ff_p=ff_p, bias=bias, scale=scale, act=act, double_drop=double_drop, mem_len=mem_len,
                      attn_cls=attn_cls) for k in range(n_layers)])
        self.mask_size = 1
    
        nn.init.normal_(self.u, 0., 0.02)
        nn.init.normal_(self.v, 0., 0.02)
        
    def forward(self, x_msk, x_lm):
        # x = encoder, y = target
        bs,lm_len = x_lm.size()
        
        msk_emb, pos_enc = self.embed(x_msk)
        lm_emb, pos_enc = self.embed(x_lm)
    
        # Masks
        lm_mask = rand_window_mask(lm_len, self.embed.mem_len, x_lm.device, 
                                    max_size=self.mask_size,p=0.3,is_eval=not self.train)
        msk_mask = None
        
        for i, layer in enumerate(self.layers):
            lm_emb = layer(msk_emb, lm_emb, msk_mask=msk_mask, lm_mask=lm_mask,
                        r=pos_enc, g_u=self.u, g_v=self.v)
        return lm_emb

class MLMEncoderBlock(nn.Module):
    "Decoder block of a Transformer model."
    #Can't use Sequential directly cause more than one input...
    def __init__(self, n_heads:int, d_model:int, d_head:int, d_inner:int, resid_p:float=0., attn_p:float=0., ff_p:float=0.,
                 bias:bool=True, scale:bool=True, double_drop:bool=True, mem_len:int=512,
                 attn_cls=MemMultiHeadRelativeAttentionKV, **kwargs):
        super().__init__()
        self.mha1 = attn_cls(n_heads, d_model, d_head, resid_p=resid_p, attn_p=attn_p, bias=bias, scale=scale, mem_len=mem_len, r_mask=False)
        self.mha2 = attn_cls(n_heads, d_model, d_head, resid_p=resid_p, attn_p=attn_p, bias=bias, scale=scale, mem_len=mem_len, r_mask=True)
        self.ff   = feed_forward(d_model, d_inner, ff_p=ff_p, double_drop=double_drop)
    
    def forward(self, enc_msk:Tensor, enc_lm:Tensor, 
                r=None, g_u=None, g_v=None,
                msk_mask:Tensor=None, lm_mask:Tensor=None): 
        y = self.mha1(enc_msk, enc_msk, enc_msk, r, g_u, g_v, mask=msk_mask)
        return self.ff(self.mha2(enc_lm, enc_msk, enc_msk, r, g_u, g_v, mask=lm_mask))
    

# LOSS AND METRICS
def acc_ignore_pad(input:Tensor, targ:Tensor, pad_idx)->Rank0Tensor:
    n = targ.shape[0]
    input = input.argmax(dim=-1).view(n,-1)
    targ = targ.view(n,-1)
    mask = targ != pad_idx
    return (input[mask]==targ[mask]).float().mean()

def mask_acc(input:Tensor, t1:Tensor, t2:Tensor)->Rank0Tensor:
    return acc_ignore_pad(input[0], t1, vocab.pad_idx)

def nw_acc(input:Tensor, t1:Tensor, t2:Tensor)->Rank0Tensor:
    x_mask, task_type, x_task = input
    if task_type[0,0].item() != TaskType.NextWord.value: return None # torch.tensor(0, device=x_mask.device).float()
    return acc_ignore_pad(x_task, t2, vocab.pad_idx)

def s2s_acc(input:Tensor, t1:Tensor, t2:Tensor)->Rank0Tensor:
    x_mask, task_type, x_task = input
    if task_type[0,0].item() != TaskType.Seq2Seq.value: return None # torch.tensor(0, device=x_mask.device).float()
    return acc_ignore_pad(x_task, t2, vocab.pad_idx)

def ns_acc(input:Tensor, t1:Tensor, t2:Tensor)->Rank0Tensor:
    x_mask, task_type, x_task = input
    if task_type[0,0].item() != TaskType.NextSent.value: return None # torch.tensor(0, device=x_mask.device).float()
    return accuracy(input[-1], t2)

