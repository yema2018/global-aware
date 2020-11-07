from att_pred_model import PreAttModel
from adabelief_pytorch import AdaBelief
import torch
import time
from generate_batch import gen_bt
import numpy as np
from transformers import BartTokenizer, BartForConditionalGeneration, get_cosine_schedule_with_warmup


class PreAtt(object):
    def __init__(self, summ_model, tokenizer, ckpt, epoch, bs):
        self.epoch = epoch
        self.tokenizer = tokenizer
        self.bs = bs
        self.summ = summ_model
        self.summ.eval()
        self.ckpt = ckpt
        for p in self.summ.parameters():
            p.requires_grad = False
        self.pre_att_model = PreAttModel(layers=1, d_model=1024, num_heads=16, dff=4096, rate=0.1)

        self.optimizer = AdaBelief(self.pre_att_model.parameters(), lr=5e-4, eps=1e-16, betas=(0.9, 0.999),
                                   weight_decay=1e-4, weight_decouple=True, rectify=True)
        # lr = get_cosine_schedule_with_warmup(torch.optim.Adam, 50000, 100000)
        # self.optimizer = torch.optim.Adam(self.pre_att_model.parameters())

        try:
            self.pre_att_model.load_state_dict(torch.load(ckpt))
            print('load {}'.format(ckpt))
        except:
            print('no checkpoints now!')

        self.loss = torch.nn.MSELoss(reduction='mean')
        self.device = torch.device('cuda:0')
        self.summ.to(self.device)
        self.pre_att_model.to(self.device)

    def pre_model(self):
        self.pre_att_model.eval()
        for p in self.pre_att_model.parameters():
            p.requires_grad = False
        return self.pre_att_model

    def cal_att_dist(self, inp, tar, inp_mask, tar_mask):
        summ_output = self.summ(input_ids=inp, attention_mask=inp_mask, decoder_input_ids=tar[:, :-1],
                                decoder_attention_mask=tar_mask[:, :-1], output_attentions=True,
                                output_hidden_states=True)

        decoder_att = summ_output[2]
        opt_att_dist = 0
        for _ in decoder_att:
            opt_att_dist += _[:, :, :, -inp.size()[1]:]
        opt_att_dist = torch.mean(torch.mean(opt_att_dist, dim=1), dim=1)
        opt_att_dist /= torch.sum(opt_att_dist, dim=1, keepdim=True)
        # print(torch.sum(opt_att_dist))
        last_encoder_hidden = summ_output[3]
        # print(inp_mask)
        pre_att_dist = self.pre_att_model(last_encoder_hidden, inp_mask)

        return opt_att_dist, pre_att_dist

    def train(self):
        for epoch in range(self.epoch):
            total_loss = []
            start_time = time.time()
            self.pre_att_model.train()
            print('start training')
            batch_set = gen_bt(self.bs, self.tokenizer, 'train', shuffle=True)
            for (batch, batch_contents) in enumerate(batch_set):
                inp, tar, inp_mask, tar_mask = batch_contents
                inp = inp.to(self.device)
                tar = tar.to(self.device)
                inp_mask = inp_mask.to(self.device)
                tar_mask = tar_mask.to(self.device)

                self.optimizer.zero_grad()

                opt_att_dist, pre_att_dist = self.cal_att_dist(inp, tar, inp_mask, tar_mask)
                # print(pre_att_dist)

                loss = self.loss(pre_att_dist.view(-1), opt_att_dist.view(-1))
                loss.backward()
                self.optimizer.step()

                total_loss.append(loss.item())
                if batch % 200 == 0 and batch > 0:
                    elapsed = time.time() - start_time
                    cur_loss = np.mean(total_loss)
                    print('| epoch {:3d} | {:5d} batches | '
                          ' ms/batch {:5.2f} | '
                          'loss {:5.4f} | ppl {:8.4f}'.format(
                        epoch, batch,
                        elapsed * 1000 / len(total_loss), cur_loss*10000, np.exp(cur_loss)))
            cur_loss = np.mean(total_loss)
            torch.save(self.pre_att_model.state_dict(), '{}_{}'.format(self.ckpt, epoch))
            elapsed = time.time() - start_time
            print('| epoch {:3d} | '
                  ' ms/epoch {:5.2f} | '
                  'loss {:5.4f} | ppl {:8.4f}'.format(
                epoch, elapsed * 1000, cur_loss*10000, np.exp(cur_loss)))
            total_loss = []
            start_time = time.time()
            print('\nstart validation')
            val_loss = []
            self.pre_att_model.eval()
            val_batch = gen_bt(self.bs, self.tokenizer, mode='val')
            for (b, bc) in enumerate(val_batch):
                inp, tar, inp_mask, tar_mask = bc
                inp = inp.to(self.device)
                tar = tar.to(self.device)
                inp_mask = inp_mask.to(self.device)
                tar_mask = tar_mask.to(self.device)
                with torch.no_grad():
                    opt_att_dist, pre_att_dist = self.cal_att_dist(inp, tar, inp_mask, tar_mask)

                    loss = self.loss(pre_att_dist.view(-1), opt_att_dist.view(-1))
                    val_loss.append(loss.item())

            print('Validation: Loss {:.8f}'.format(np.mean(val_loss)*10000))

    def pred(self, inp, masks):
        self.pre_att_model.eval()
        return self.pre_att_model(inp, masks)


if __name__ == '__main__':
    bart = BartForConditionalGeneration.from_pretrained('facebook/bart-large-cnn', use_cache=False)
    tokenizer = BartTokenizer.from_pretrained('facebook/bart-large-cnn')
    a = PreAtt(bart, tokenizer, './cnndm/layer_1', 10, 3)
    a.train()

