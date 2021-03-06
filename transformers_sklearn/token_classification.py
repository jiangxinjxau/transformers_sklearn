import os
import torch
import random
import logging
import numpy as np
from tqdm import tqdm, trange
from torch.nn import CrossEntropyLoss
from torch.utils.data import random_split,TensorDataset,\
    DistributedSampler,RandomSampler,SequentialSampler,DataLoader
from tensorboardX import SummaryWriter
from transformers_sklearn.utils.token_classification_utils import get_labels,\
    read_examples_from_X_y,convert_examples_to_features
from transformers_sklearn.utils.data_utils import to_numpy
from sklearn.base import BaseEstimator,ClassifierMixin
from transformers_sklearn.utils.metrics_utils import f1_score,recall_score,precision_score,classification_report


from transformers import AdamW, get_linear_schedule_with_warmup
from transformers import BertConfig, BertForTokenClassification, BertTokenizer
from transformers import RobertaConfig, RobertaForTokenClassification, RobertaTokenizer
from transformers import DistilBertConfig, DistilBertForTokenClassification, DistilBertTokenizer
from transformers import CamembertConfig, CamembertForTokenClassification, CamembertTokenizer
from transformers import XLMRobertaConfig,XLMRobertaForTokenClassification,XLMRobertaTokenizer

# from transformers import AlbertConfig,AlbertTokenizer
from transformers_sklearn.model_albert_fix import AlbertForTokenClassification,\
    AlbertTokenizer,AlbertConfig,BrightAlbertForTokenClassification

from transformers_sklearn.model_electra import ElectraConfig,ElectraForTokenClassification,ElectraTokenizer

from transformers import BERT_PRETRAINED_CONFIG_ARCHIVE_MAP, XLNET_PRETRAINED_CONFIG_ARCHIVE_MAP, \
    XLM_PRETRAINED_CONFIG_ARCHIVE_MAP, ROBERTA_PRETRAINED_CONFIG_ARCHIVE_MAP, DISTILBERT_PRETRAINED_CONFIG_ARCHIVE_MAP
ALL_MODELS = sum((tuple(conf.keys()) for conf in (BERT_PRETRAINED_CONFIG_ARCHIVE_MAP,
                                                                                XLNET_PRETRAINED_CONFIG_ARCHIVE_MAP,
                                                                                XLM_PRETRAINED_CONFIG_ARCHIVE_MAP,
                                                                                ROBERTA_PRETRAINED_CONFIG_ARCHIVE_MAP,
                                                                                DISTILBERT_PRETRAINED_CONFIG_ARCHIVE_MAP)), ())

MODEL_CLASSES = {
    "bert": (BertConfig, BertForTokenClassification, BertTokenizer),
    "roberta": (RobertaConfig, RobertaForTokenClassification, RobertaTokenizer),
    "distilbert": (DistilBertConfig, DistilBertForTokenClassification, DistilBertTokenizer),
    "camembert": (CamembertConfig, CamembertForTokenClassification, CamembertTokenizer),
    "albert":(AlbertConfig,AlbertForTokenClassification,AlbertTokenizer),
    "bright_albert": (AlbertConfig,BrightAlbertForTokenClassification,AlbertTokenizer),
    "xlmroberta": (XLMRobertaConfig, XLMRobertaForTokenClassification, XLMRobertaTokenizer),
    "electra": (ElectraConfig,ElectraForTokenClassification,ElectraTokenizer)
}

logger = logging.getLogger(__name__)

def set_seed(seed=520,n_gpu=1):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if n_gpu > 0:
        torch.cuda.manual_seed_all(seed)

class BERTologyNERClassifer(BaseEstimator,ClassifierMixin):

    def __init__(self,labels,data_dir='ts_data',model_type='bert',
                 model_name_or_path='bert-base-chinese',
                 output_dir='ts_results',config_name='',
                 tokenizer_name='',cache_dir='model_cache',
                 max_seq_length=512,do_lower_case=False,
                 per_gpu_train_batch_size=8,per_gpu_eval_batch_size=8,
                 gradient_accumulation_steps=1,
                 learning_rate=5e-5,weight_decay=0.0,
                 adam_epsilon=1e-8,max_grad_norm=1.0,
                 num_train_epochs=3.0,max_steps=-1,
                 warmup_steps=0,logging_steps=50,
                 save_steps=50,evaluate_during_training=True,
                 no_cuda=False,overwrite_output_dir=False,
                 overwrite_cache=False,seed=520,
                 fp16=False,fp16_opt_level='01',
                 local_rank=-1,val_fraction=0.1):
        self.labels = labels
        self.data_dir = data_dir
        self.model_type = model_type
        self.model_name_or_path = model_name_or_path
        self.output_dir = output_dir
        self.config_name = config_name
        self.tokenizer_name = tokenizer_name
        self.max_seq_length = max_seq_length
        self.do_lower_case = do_lower_case
        self.cache_dir = cache_dir
        self.per_gpu_train_batch_size = per_gpu_train_batch_size
        self.per_gpu_eval_batch_size = per_gpu_eval_batch_size
        self.gradient_accumulation_steps = gradient_accumulation_steps
        self.learning_rate = learning_rate
        self.weight_decay = weight_decay
        self.adam_epsilon = adam_epsilon
        self.max_grad_norm = max_grad_norm
        self.num_train_epochs = num_train_epochs
        self.max_steps = max_steps
        self.warmup_steps = warmup_steps
        self.logging_steps = logging_steps
        self.save_steps = save_steps
        self.evaluate_during_training = evaluate_during_training
        self.no_cuda = no_cuda
        self.overwrite_output_dir = overwrite_output_dir
        self.overwrite_cache = overwrite_cache
        self.seed = seed
        self.fp16 = fp16
        self.fp16_opt_level = fp16_opt_level
        self.local_rank = local_rank
        self.val_fraction = val_fraction

        self.id2label = {i: label for i, label in enumerate(self.labels)}
        self.label_map = {label: i  for i, label in enumerate(self.labels)}

        # Setup CUDA, GPU & distributed training
        if self.local_rank == -1 or self.no_cuda:
            device = torch.device("cuda" if torch.cuda.is_available() and not self.no_cuda else "cpu")
            self.n_gpu = torch.cuda.device_count() if not self.no_cuda else 1
        else:  # Initializes the distributed backend which will take care of sychronizing nodes/GPUs
            torch.cuda.set_device(self.local_rank)
            device = torch.device("cuda", self.local_rank)
            torch.distributed.init_process_group(backend="nccl")
            self.n_gpu = 1
        self.device = device

        # Setup logging
        logging.basicConfig(format="%(asctime)s - %(levelname)s - %(name)s -   %(message)s",
                            datefmt="%m/%d/%Y %H:%M:%S",
                            level=logging.INFO if self.local_rank in [-1, 0] else logging.WARN)
        logger.warning("Process rank: %s, device: %s, n_gpu: %s, distributed training: %s, 16-bits training: %s",
                       self.local_rank, device, self.n_gpu, bool(self.local_rank != -1), self.fp16)

        # Set seed
        set_seed(seed=self.seed,n_gpu=self.n_gpu)


    def fit(self,X,y):
        if not os.path.exists(self.data_dir):
            os.makedirs(self.data_dir)

        if not os.path.exists(self.output_dir):
            os.makedirs(self.output_dir)

        if os.path.exists(self.output_dir) and os.listdir(
                self.output_dir) and not self.overwrite_output_dir:
            raise ValueError(
                "Output directory ({}) already exists and is not empty. Use --overwrite_output_dir to overcome.".format(
                    self.output_dir))

        num_labels = len(self.labels)
        # self.labels = labels
        # Use cross entropy ignore index as padding label id so that only real label ids contribute to the loss later
        pad_token_label_id = CrossEntropyLoss().ignore_index
        self.pad_token_label_id = pad_token_label_id
        # Load pretrained model and tokenizer
        if self.local_rank not in [-1, 0]:
            torch.distributed.barrier()  # Make sure only the first process in distributed training will download model & vocab

        self.model_type = self.model_type.lower()
        config_class, model_class, tokenizer_class = MODEL_CLASSES[self.model_type]
        config = config_class.from_pretrained(self.config_name if self.config_name else self.model_name_or_path,
                                              num_labels=num_labels,
                                              cache_dir=self.cache_dir if self.cache_dir else None,
                                              share_type='all' if self.model_type=='albert' else None)
        tokenizer = tokenizer_class.from_pretrained(
            self.tokenizer_name if self.tokenizer_name else self.model_name_or_path,
            do_lower_case=self.do_lower_case,
            cache_dir=self.cache_dir if self.cache_dir else None)
        model = model_class.from_pretrained(self.model_name_or_path,
                                            from_tf=bool(".ckpt" in self.model_name_or_path),
                                            config=config,
                                            cache_dir=self.cache_dir if self.cache_dir else None)

        if self.local_rank == 0:
            torch.distributed.barrier()  # Make sure only the first process in distributed training will download model & vocab

        model.to(self.device)

        logger.info("Training/evaluation parameters %s", self)

        train_dataset = load_and_cache_examples(self, tokenizer, pad_token_label_id, X,y, mode="train")
        global_step, tr_loss = train(self, train_dataset,model,tokenizer,pad_token_label_id)
        logger.info(" global_step = %s, average loss = %s", global_step, tr_loss)

        # Saving best-practices: if you use defaults names for the model, you can reload it using from_pretrained()
        if self.local_rank == -1 or torch.distributed.get_rank() == 0:
            # Create output directory if needed
            if not os.path.exists(self.output_dir) and self.local_rank in [-1, 0]:
                os.makedirs(self.output_dir)

            logger.info("Saving model checkpoint to %s", self.output_dir)
            # Save a trained model, configuration and tokenizer using `save_pretrained()`.
            # They can then be reloaded using `from_pretrained()`
            model_to_save = model.module if hasattr(model,"module") else model  # Take care of distributed/parallel training
            model_to_save.save_pretrained(self.output_dir)
            tokenizer.save_pretrained(self.output_dir)

            # Good practice: save your training arguments together with the trained model
            torch.save(self, os.path.join(self.output_dir, "training_args.bin"))

        return self

    def predict(self,X):
        # args = torch.load(os.path.join(self.output_dir, "training_args.bin"))
        # Load a trained model and vocabulary that you have fine-tuned
        _, model_class, tokenizer_class = MODEL_CLASSES[self.model_type]
        model = model_class.from_pretrained(self.output_dir)
        tokenizer = tokenizer_class.from_pretrained(self.output_dir)

        model.to(self.device)

        pad_token_label_id = CrossEntropyLoss().ignore_index
        # get dataset
        test_dataset = load_and_cache_examples(self,tokenizer,pad_token_label_id,X,y=None,mode='test')

        _, preds_list = evaluate(self,test_dataset,model,pad_token_label_id,mode='test')

        return preds_list

    def score(self, X, y, sample_weight=None):
        y_pred = self.predict(X)
        return classification_report(y,y_pred,digits=4)


def load_and_cache_examples(args, tokenizer,pad_token_label_id, X,y,mode):
    if args.local_rank not in [-1, 0] and mode=='train':
        torch.distributed.barrier()  # Make sure only the first process in distributed training process the dataset, and the others will use the cache

    # Load datasets features from cache or dataset file
    cached_features_file = os.path.join(args.data_dir, "cached_{}_{}_{}".format(mode,
        args.model_type,
        str(args.max_seq_length)))
    if os.path.exists(cached_features_file) and not args.overwrite_cache and mode=='train':
        logger.info("Loading features from cached file %s", cached_features_file)
        features = torch.load(cached_features_file)
    else:
        logger.info("Creating features from dataset file at %s", args.data_dir)
        examples = read_examples_from_X_y(X,y, mode)
        features = convert_examples_to_features(examples, args.label_map, args.max_seq_length, tokenizer,
                                                cls_token_at_end=bool(args.model_type in ["xlnet"]),
                                                # xlnet has a cls token at the end
                                                cls_token=tokenizer.cls_token,
                                                cls_token_segment_id=2 if args.model_type in ["xlnet"] else 0,
                                                sep_token=tokenizer.sep_token,
                                                sep_token_extra=bool(args.model_type in ["roberta"]),
                                                # roberta uses an extra separator b/w pairs of sentences, cf. github.com/pytorch/fairseq/commit/1684e166e3da03f5b600dbb7855cb98ddfcd0805
                                                pad_on_left=bool(args.model_type in ["xlnet"]),
                                                # pad on the left for xlnet
                                                pad_token=tokenizer.convert_tokens_to_ids([tokenizer.pad_token])[0],
                                                pad_token_segment_id=4 if args.model_type in ["xlnet"] else 0,
                                                pad_token_label_id=pad_token_label_id
                                                )
        if args.local_rank in [-1, 0] and mode == 'train':
            logger.info("Saving features into cached file %s", cached_features_file)
            torch.save(features, cached_features_file)

    if args.local_rank == 0 and mode =='train':
        torch.distributed.barrier()  # Make sure only the first process in distributed training process the dataset, and the others will use the cache

    # Convert to Tensors and build dataset
    all_input_ids = torch.tensor([f.input_ids for f in features], dtype=torch.long)
    all_input_mask = torch.tensor([f.input_mask for f in features], dtype=torch.long)
    all_segment_ids = torch.tensor([f.segment_ids for f in features], dtype=torch.long)
    all_label_ids = torch.tensor([f.label_ids for f in features], dtype=torch.long)

    dataset = TensorDataset(all_input_ids, all_input_mask, all_segment_ids, all_label_ids)
    return dataset

def train(args, train_dataset, model, tokenizer, pad_token_label_id):
    """ Train the model """
    if args.local_rank in [-1, 0]:
        tb_writer = SummaryWriter()

    args.train_batch_size = args.per_gpu_train_batch_size * max(1, args.n_gpu)

    val_len = int(len(train_dataset)*args.val_fraction)
    train_len = len(train_dataset) - val_len
    train_ds, val_ds = random_split(train_dataset,[train_len,val_len])
    train_sampler = RandomSampler(train_ds) if args.local_rank == -1 else DistributedSampler(train_ds)
    train_dataloader = DataLoader(train_ds, sampler=train_sampler, batch_size=args.train_batch_size)

    if args.max_steps > 0:
        t_total = args.max_steps
        args.num_train_epochs = args.max_steps // (len(train_dataloader) // args.gradient_accumulation_steps) + 1
    else:
        t_total = len(train_dataloader) // args.gradient_accumulation_steps * args.num_train_epochs

    # Prepare optimizer and schedule (linear warmup and decay)
    no_decay = ["bias", "LayerNorm.weight"]
    optimizer_grouped_parameters = [
        {"params": [p for n, p in model.named_parameters() if not any(nd in n for nd in no_decay)],
         "weight_decay": args.weight_decay},
        {"params": [p for n, p in model.named_parameters() if any(nd in n for nd in no_decay)], "weight_decay": 0.0}
    ]
    optimizer = AdamW(optimizer_grouped_parameters, lr=args.learning_rate, eps=args.adam_epsilon)
    scheduler = get_linear_schedule_with_warmup(optimizer, num_warmup_steps=args.warmup_steps, num_training_steps=t_total)

    # Check if saved optimizer or scheduler states exist
    if os.path.isfile(os.path.join(args.model_name_or_path, "optimizer.pt")) and os.path.isfile(
            os.path.join(args.model_name_or_path, "scheduler.pt")
    ):
        # Load in optimizer and scheduler states
        optimizer.load_state_dict(torch.load(os.path.join(args.model_name_or_path, "optimizer.pt")))
        scheduler.load_state_dict(torch.load(os.path.join(args.model_name_or_path, "scheduler.pt")))

    if args.fp16:
        try:
            from apex import amp
        except ImportError:
            raise ImportError("Please install apex from https://www.github.com/nvidia/apex to use fp16 training.")
        model, optimizer = amp.initialize(model, optimizer, opt_level=args.fp16_opt_level)

    # multi-gpu training (should be after apex fp16 initialization)
    if args.n_gpu > 1:
        model = torch.nn.DataParallel(model)

    # Distributed training (should be after apex fp16 initialization)
    if args.local_rank != -1:
        model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[args.local_rank],
                                                          output_device=args.local_rank,
                                                          find_unused_parameters=True)

    # Train!
    logger.info("***** Running training *****")
    logger.info("  Num examples = %d", len(train_ds))
    logger.info("  Num Epochs = %d", args.num_train_epochs)
    logger.info("  Instantaneous batch size per GPU = %d", args.per_gpu_train_batch_size)
    logger.info("  Total train batch size (w. parallel, distributed & accumulation) = %d",
                args.train_batch_size * args.gradient_accumulation_steps * (
                    torch.distributed.get_world_size() if args.local_rank != -1 else 1))
    logger.info("  Gradient Accumulation steps = %d", args.gradient_accumulation_steps)
    logger.info("  Total optimization steps = %d", t_total)

    global_step = 0
    epochs_trained = 0
    steps_trained_in_current_epoch = 0
    # Check if continuing training from a checkpoint
    if os.path.exists(args.model_name_or_path):
        # set global_step to gobal_step of last saved checkpoint from model path
        global_step = int(args.model_name_or_path.split("-")[-1].split("/")[0])
        epochs_trained = global_step // (len(train_dataloader) // args.gradient_accumulation_steps)
        steps_trained_in_current_epoch = global_step % (len(train_dataloader) // args.gradient_accumulation_steps)

        logger.info("  Continuing training from checkpoint, will skip to saved global_step")
        logger.info("  Continuing training from epoch %d", epochs_trained)
        logger.info("  Continuing training from global step %d", global_step)
        logger.info("  Will skip the first %d steps in the first epoch", steps_trained_in_current_epoch)

    tr_loss, logging_loss = 0.0, 0.0
    model.zero_grad()
    train_iterator = trange(int(args.num_train_epochs), desc="Epoch", disable=args.local_rank not in [-1, 0])
    set_seed(seed=args.seed,n_gpu=args.n_gpu)  # Added here for reproductibility (even between python 2 and 3)
    for _ in train_iterator:
        epoch_iterator = tqdm(train_dataloader, desc="Iteration", disable=args.local_rank not in [-1, 0])
        for step, batch in enumerate(epoch_iterator):

            # Skip past any already trained steps if resuming training
            if steps_trained_in_current_epoch > 0:
                steps_trained_in_current_epoch -= 1
                continue

            model.train()
            batch = tuple(t.to(args.device) for t in batch)
            inputs = {"input_ids": batch[0],
                      "attention_mask": batch[1],
                      "labels": batch[3]}
            if args.model_type != "distilbert":
                inputs["token_type_ids"] = batch[2] if args.model_type in ["bert", "xlnet"] else None  # XLM and RoBERTa don"t use segment_ids

            outputs = model(**inputs)
            loss = outputs[0]  # model outputs are always tuple in pytorch-transformers (see doc)

            if args.n_gpu > 1:
                loss = loss.mean()  # mean() to average on multi-gpu parallel training
            if args.gradient_accumulation_steps > 1:
                loss = loss / args.gradient_accumulation_steps

            if args.fp16:
                with amp.scale_loss(loss, optimizer) as scaled_loss:
                    scaled_loss.backward()
            else:
                loss.backward()

            tr_loss += loss.item()
            if (step + 1) % args.gradient_accumulation_steps == 0:
                if args.fp16:
                    torch.nn.utils.clip_grad_norm_(amp.master_params(optimizer), args.max_grad_norm)
                else:
                    torch.nn.utils.clip_grad_norm_(model.parameters(), args.max_grad_norm)

                optimizer.step()
                scheduler.step()  # Update learning rate schedule
                model.zero_grad()
                global_step += 1

                if args.local_rank in [-1, 0] and args.logging_steps > 0 and global_step % args.logging_steps == 0:
                    # Log metrics
                    if args.local_rank == -1 and args.evaluate_during_training:  # Only evaluate when single GPU otherwise metrics may not average well
                        results, _ = evaluate(args, val_ds, model,pad_token_label_id,prefix=global_step)
                        for key, value in results.items():
                            tb_writer.add_scalar("eval_{}".format(key), value, global_step)
                    tb_writer.add_scalar("lr", scheduler.get_lr()[0], global_step)
                    tb_writer.add_scalar("loss", (tr_loss - logging_loss) / args.logging_steps, global_step)
                    logging_loss = tr_loss

                if args.local_rank in [-1, 0] and args.save_steps > 0 and global_step % args.save_steps == 0:
                    # Save model checkpoint
                    output_dir = os.path.join(args.output_dir, "checkpoint-{}".format(global_step))
                    if not os.path.exists(output_dir):
                        os.makedirs(output_dir)
                    model_to_save = model.module if hasattr(model, "module") else model  # Take care of distributed/parallel training
                    model_to_save.save_pretrained(output_dir)
                    tokenizer.save_pretrained(output_dir)

                    torch.save(args, os.path.join(output_dir, "training_args.bin"))
                    logger.info("Saving model checkpoint to %s", output_dir)

                    torch.save(optimizer.state_dict(), os.path.join(output_dir, "optimizer.pt"))
                    torch.save(scheduler.state_dict(), os.path.join(output_dir, "scheduler.pt"))
                    logger.info("Saving optimizer and scheduler states to %s", output_dir)

            if args.max_steps > 0 and global_step > args.max_steps:
                epoch_iterator.close()
                break
        if args.max_steps > 0 and global_step > args.max_steps:
            train_iterator.close()
            break

    if args.local_rank in [-1, 0]:
        tb_writer.close()

    return global_step, tr_loss / global_step


def evaluate(args, eval_dataset, model, pad_token_label_id, mode='dev',prefix=0):
    # eval_dataset = load_and_cache_examples(args, tokenizer, labels, pad_token_label_id, mode=mode)

    args.eval_batch_size = args.per_gpu_eval_batch_size * max(1, args.n_gpu)
    # Note that DistributedSampler samples randomly
    eval_sampler = SequentialSampler(eval_dataset) if args.local_rank == -1 else DistributedSampler(eval_dataset)
    eval_dataloader = DataLoader(eval_dataset, sampler=eval_sampler, batch_size=args.eval_batch_size)

    # multi-gpu evaluate
    if args.n_gpu > 1 and mode == 'test':
        model = torch.nn.DataParallel(model)

    # Eval!
    if mode == 'dev':
        logger.info("***** Running evaluation %s *****", prefix)
    else:
        logger.info("***** Running predict *****")
    logger.info("  Num examples = %d", len(eval_dataset))
    logger.info("  Batch size = %d", args.eval_batch_size)
    eval_loss = 0.0
    nb_eval_steps = 0
    preds = None
    out_label_ids = None
    model.eval()
    for batch in tqdm(eval_dataloader, desc="Evaluating" if mode=='dev' else "Predicting"):
        batch = tuple(t.to(args.device) for t in batch)

        with torch.no_grad():
            inputs = {"input_ids": batch[0],"attention_mask": batch[1],"labels": batch[3]}
            if args.model_type != "distilbert":
                inputs["token_type_ids"] = batch[2] if args.model_type in ["bert", "xlnet"] else None  # XLM and RoBERTa don"t use segment_ids
            outputs = model(**inputs)
            tmp_eval_loss, logits = outputs[:2]

            if args.n_gpu > 1:
                tmp_eval_loss = tmp_eval_loss.mean()  # mean() to average on multi-gpu parallel evaluating

            eval_loss += tmp_eval_loss.item()
        nb_eval_steps += 1
        if preds is None:
            preds = logits.detach().cpu().numpy()
            out_label_ids = inputs["labels"].detach().cpu().numpy()
        else:
            preds = np.append(preds, logits.detach().cpu().numpy(), axis=0)
            out_label_ids = np.append(out_label_ids, inputs["labels"].detach().cpu().numpy(), axis=0)

    eval_loss = eval_loss / nb_eval_steps
    preds = np.argmax(preds, axis=2)

    # label_map = {i: label for i, label in enumerate(labels)}

    out_label_list = [[] for _ in range(out_label_ids.shape[0])]
    preds_list = [[] for _ in range(out_label_ids.shape[0])]

    for i in range(out_label_ids.shape[0]):
        for j in range(out_label_ids.shape[1]):
            if out_label_ids[i, j] != pad_token_label_id:
                out_label_list[i].append(args.id2label[out_label_ids[i][j]])
                preds_list[i].append(args.id2label[preds[i][j]])

    results = {
        "loss": eval_loss,
        "precision": precision_score(out_label_list, preds_list),
        "recall": recall_score(out_label_list, preds_list),
        "f1": f1_score(out_label_list, preds_list)
    }

    if mode == 'dev':
        output_eval_file = os.path.join(args.output_dir, "eval_results.txt")
        with open(output_eval_file, "a") as writer:
            logger.info("***** Eval results %d *****",prefix)
            writer.write("***** Eval results {} *****".format(prefix))
            for key in sorted(results.keys()):
                msg = "{} = {}".format(key, str(results[key]))
                logger.info(msg)
                writer.write(msg)
                writer.write('\n')
            writer.write('\n')

    return results, preds_list